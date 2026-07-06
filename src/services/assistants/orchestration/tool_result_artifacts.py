from __future__ import annotations

import base64
import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from component.config import settings


TEXT_KEYS = ("content_text", "content", "text", "data")
BINARY_KEYS = ("file_bytes", "bytes", "base64", "blob_base64")


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "unknown")).strip("_") or "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_probably_base64(value: str) -> bool:
    if not isinstance(value, str) or len(value) < 128 or len(value) % 4 != 0:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
        return False
    return True


def _read_text_preview(path: Path, max_chars: int) -> Tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return (text[:max_chars], len(text) > max_chars)


class ToolResultNormalizer:
    def __init__(self, *, thread_id: Optional[str], run_id: Optional[str]):
        self.thread_id = _safe_slug(thread_id or "session")
        self.run_id = _safe_slug(run_id or "run")
        self.ask_root = Path(settings.ASK_JOB_ROOT)

    def _artifact_dir(self, tool_call_id: str) -> Path:
        return self.ask_root / self.thread_id / self.run_id / "artifacts" / _safe_slug(tool_call_id)

    def _write_artifact_bytes(self, *, tool_call_id: str, tool: str, data: bytes, filename: str, mime_type: Optional[str], truncated_for_llm: bool, inspection_level: str) -> Dict[str, Any]:
        out_dir = self._artifact_dir(tool_call_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / filename
        file_path.write_bytes(data)
        guessed_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        artifact = {
            "artifact_id": uuid.uuid4().hex,
            "tool_call_id": tool_call_id,
            "tool": tool,
            "source_path": None,
            "local_path": str(file_path),
            "content_ref": f"artifact://{self.thread_id}/{self.run_id}/{_safe_slug(tool_call_id)}/{filename}",
            "mime_type": guessed_mime,
            "size_bytes": len(data),
            "created_at": _now_iso(),
            "truncated_for_llm": truncated_for_llm,
            "inspection_level": inspection_level,
        }
        (out_dir / "metadata.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact

    def normalize(self, *, tool_call: Dict[str, Any], raw_result: Any) -> Dict[str, Any]:
        tool = (tool_call.get("tool") or "").strip()
        tool_call_id = str(tool_call.get("tool_id") or tool or "tool_call")
        envelope: Dict[str, Any] = {
            "status": "success",
            "tool": tool,
            "summary": "Tool executed.",
            "facts": {},
            "artifacts": [],
            "preview": None,
            "content_text": None,
            "content_ref": None,
            "local_path": None,
            "mime_type": None,
            "size_bytes": None,
            "truncated": False,
            "full_content_available_to_llm": False,
            "inspection_level": "metadata_only",
            "raw_result": raw_result,
        }

        if isinstance(raw_result, dict) and raw_result.get("error"):
            error_message = str(raw_result.get("error"))[:500]
            envelope.update({
                "status": "failed",
                "summary": error_message,
                "inspection_level": "not_available",
                "error_type": raw_result.get("error_type") or "tool_exception",
                "message": error_message,
                "recoverable": raw_result.get("recoverable", True),
            })
            if raw_result.get("exit_code") is not None:
                envelope["exit_code"] = raw_result.get("exit_code")
            return envelope

        if isinstance(raw_result, dict) and raw_result.get("exit_code") not in (None, 0):
            stdout = str(raw_result.get("stdout") or "")
            stderr = str(raw_result.get("stderr") or "")
            exit_code = raw_result.get("exit_code")
            message = (stderr or stdout or f"Command exited with code {exit_code}").strip()[:500]
            envelope.update({
                "status": "failed",
                "summary": message,
                "inspection_level": "not_available",
                "error_type": "command_failed",
                "message": message,
                "exit_code": exit_code,
                "stdout_preview": stdout[:1000],
                "stderr_preview": stderr[:1000],
                "recoverable": True,
            })
            return envelope

        # Known local path
        if isinstance(raw_result, dict) and (raw_result.get("local_path") or raw_result.get("path")):
            local_path = Path(raw_result.get("local_path") or raw_result.get("path"))
            if local_path.exists() and local_path.is_file():
                size = local_path.stat().st_size
                mime = raw_result.get("mime_type") or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
                envelope.update({"local_path": str(local_path), "mime_type": mime, "size_bytes": size})
                if mime.startswith("text/") and size <= settings.TOOL_RESULT_MAX_INLINE_BYTES:
                    text = local_path.read_text(encoding="utf-8", errors="replace")
                    if len(text) <= settings.TOOL_RESULT_MAX_INLINE_CHARS:
                        envelope.update({"content_text": text, "inspection_level": "full_inline", "full_content_available_to_llm": True, "summary": "Text file available inline."})
                    else:
                        preview, _ = _read_text_preview(local_path, settings.TOOL_RESULT_MAX_PREVIEW_CHARS)
                        envelope.update({"preview": preview, "truncated": True, "inspection_level": "preview_only", "summary": "Only preview provided; use file_* tools for deeper inspection."})
                else:
                    envelope.update({"inspection_level": "artifact_only", "summary": "Binary/large file result stored as artifact reference."})
                envelope["content_ref"] = raw_result.get("content_ref")
                return envelope

        text_candidate = None
        if isinstance(raw_result, dict):
            for key in TEXT_KEYS:
                value = raw_result.get(key)
                if isinstance(value, str) and value:
                    text_candidate = value
                    break

        if text_candidate is not None:
            encoded_len = len(text_candidate.encode("utf-8", errors="replace"))
            if len(text_candidate) <= settings.TOOL_RESULT_MAX_INLINE_CHARS and encoded_len <= settings.TOOL_RESULT_MAX_INLINE_BYTES:
                envelope.update({"content_text": text_candidate, "inspection_level": "full_inline", "full_content_available_to_llm": True, "size_bytes": encoded_len, "summary": "Tool returned inline text content."})
            else:
                artifact = self._write_artifact_bytes(
                    tool_call_id=tool_call_id,
                    tool=tool,
                    data=text_candidate.encode("utf-8", errors="replace"),
                    filename="tool_output.txt",
                    mime_type="text/plain",
                    truncated_for_llm=True,
                    inspection_level="preview_only",
                )
                envelope["artifacts"].append(artifact)
                envelope.update({
                    "preview": text_candidate[: settings.TOOL_RESULT_MAX_PREVIEW_CHARS],
                    "content_ref": artifact["content_ref"],
                    "local_path": artifact["local_path"],
                    "mime_type": artifact["mime_type"],
                    "size_bytes": artifact["size_bytes"],
                    "truncated": True,
                    "inspection_level": "preview_only",
                    "summary": "Large text stored as artifact; only preview provided. Use file_* tools.",
                })
            return envelope

        if isinstance(raw_result, dict):
            for key in BINARY_KEYS:
                v = raw_result.get(key)
                if isinstance(v, str) and _is_probably_base64(v):
                    decoded = base64.b64decode(v)
                    artifact = self._write_artifact_bytes(
                        tool_call_id=tool_call_id,
                        tool=tool,
                        data=decoded,
                        filename=raw_result.get("filename") or "tool_output.bin",
                        mime_type=raw_result.get("mime_type"),
                        truncated_for_llm=False,
                        inspection_level="artifact_only",
                    )
                    envelope["artifacts"].append(artifact)
                    envelope.update({
                        "content_ref": artifact["content_ref"], "local_path": artifact["local_path"],
                        "mime_type": artifact["mime_type"], "size_bytes": artifact["size_bytes"],
                        "inspection_level": "artifact_only", "summary": "Binary content stored as artifact.",
                    })
                    return envelope

        compact = raw_result
        if isinstance(raw_result, (dict, list)):
            rendered = json.dumps(raw_result, ensure_ascii=False)
            # Inline the WHOLE structured result up to the same limit as inline text (was
            # only 8k = MAX_PREVIEW_CHARS, which truncated normal documents/search results
            # into an artifact the agent then couldn't read). When the full output fits, mark
            # it full_inline + full_content_available_to_llm so the agent trusts it and stops
            # trying to "inspect" further. Only genuinely huge results become a preview_only
            # artifact.
            if len(rendered) <= settings.TOOL_RESULT_MAX_INLINE_CHARS:
                envelope.update({
                    "facts": raw_result if isinstance(raw_result, dict) else {"items": raw_result},
                    "inspection_level": "full_inline",
                    "full_content_available_to_llm": True,
                    "summary": "Full structured result inline.",
                })
            else:
                artifact = self._write_artifact_bytes(
                    tool_call_id=tool_call_id,
                    tool=tool,
                    data=rendered.encode("utf-8"),
                    filename="tool_output.json",
                    mime_type="application/json",
                    truncated_for_llm=True,
                    inspection_level="preview_only",
                )
                envelope["artifacts"].append(artifact)
                envelope.update({"preview": rendered[: settings.TOOL_RESULT_MAX_PREVIEW_CHARS], "content_ref": artifact["content_ref"], "local_path": artifact["local_path"], "mime_type": artifact["mime_type"], "size_bytes": artifact["size_bytes"], "truncated": True, "inspection_level": "preview_only", "summary": "Large structured output truncated; inspect artifact with file_* tools."})
        else:
            envelope["facts"] = {"value": str(compact)[:500]}
        return envelope
