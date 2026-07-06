# services/voice_utilities.py
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from component.logging import get_logger


log = get_logger(__name__)


# -----------------------------
# Time + formatting
# -----------------------------
def utc_iso() -> str:
    result = datetime.now(timezone.utc).isoformat()
    log.debugx(
        "voice_utilities:utc_iso",
        result=result,
    )
    return result


def utc_stamp() -> str:
    result = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    log.debugx(
        "voice_utilities:utc_stamp",
        result=result,
    )
    return result


def sec_to_mmss(s: Optional[float]) -> str:
    log.debugx(
        "voice_utilities:sec_to_mmss:start",
        value=s,
        value_type=type(s).__name__,
    )
    if s is None:
        log.debugx("voice_utilities:sec_to_mmss:none")
        return ""
    try:
        s = float(s)
    except Exception:
        log.debugx(
            "voice_utilities:sec_to_mmss:invalid_float",
            value=s,
            value_type=type(s).__name__,
        )
        return ""
    s = max(0.0, s)
    m = int(s // 60)
    sec = int(s % 60)
    result = f"{m:02d}:{sec:02d}"
    log.debugx(
        "voice_utilities:sec_to_mmss:done",
        seconds=s,
        result=result,
    )
    return result


def fmt_timerange(start_s: Optional[float], end_s: Optional[float]) -> str:
    log.debugx(
        "voice_utilities:fmt_timerange:start",
        start_s=start_s,
        end_s=end_s,
    )
    a = sec_to_mmss(start_s)
    b = sec_to_mmss(end_s)
    if a and b:
        result = f"[{a}–{b}]"
        log.debugx(
            "voice_utilities:fmt_timerange:range",
            result=result,
        )
        return result
    if a:
        result = f"[{a}]"
        log.debugx(
            "voice_utilities:fmt_timerange:start_only",
            result=result,
        )
        return result
    log.debugx("voice_utilities:fmt_timerange:empty")
    return ""


def safe_slug(s: str, *, max_len: int = 80, fallback: str = "id") -> str:
    log.debugx(
        "voice_utilities:safe_slug:start",
        input_len=len(s or ""),
        max_len=max_len,
        fallback=fallback,
    )
    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("._-")
    result = (s or fallback)[:max_len]
    log.debugx(
        "voice_utilities:safe_slug:done",
        result=result,
        result_len=len(result),
        used_fallback=not bool(s),
    )
    return result


# -----------------------------
# Mermaid mindmap normalizer
# -----------------------------
def normalize_mermaid_mindmap(mm_content: str) -> str:
    """
    Best-effort normalizer for Mermaid mindmap syntax.
    Keeps behavior compatible with your existing implementation.
    """
    log.infox(
        "voice_utilities:normalize_mermaid_mindmap:start",
        content_len=len(mm_content or ""),
    )

    raw_lines = [ln.rstrip() for ln in (mm_content or "").splitlines()]

    log.debugx(
        "voice_utilities:normalize_mermaid_mindmap:raw_lines",
        raw_line_count=len(raw_lines),
    )

    while raw_lines and not raw_lines[0].strip():
        raw_lines.pop(0)
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()

    if not raw_lines:
        log.debugx("voice_utilities:normalize_mermaid_mindmap:empty")
        return "mindmap"

    if raw_lines[0].strip() != "mindmap":
        log.debugx(
            "voice_utilities:normalize_mermaid_mindmap:missing_header",
            first_line=raw_lines[0].strip() if raw_lines else None,
        )
        for i, ln in enumerate(raw_lines):
            if ln.strip() == "mindmap":
                raw_lines = raw_lines[i:]
                log.debugx(
                    "voice_utilities:normalize_mermaid_mindmap:header_found_later",
                    index=i,
                )
                break
        else:
            raw_lines.insert(0, "mindmap")
            log.debugx("voice_utilities:normalize_mermaid_mindmap:header_inserted")

    out: List[str] = []
    for ln in raw_lines:
        if ln.strip() == "mindmap":
            out.append("mindmap")
            continue

        ln = ln.replace("\t", "  ")
        indent = len(ln) - len(ln.lstrip(" "))
        text = ln.strip()
        if not text:
            continue

        m = re.match(r"^(root\s*\(\(.*?\)\))(.*)$", text)
        if m and m.group(2).strip():
            root_node = m.group(1).strip()
            tail = m.group(2).strip()
            out.append(" " * indent + root_node)

            parts = [p.strip() for p in re.split(r"\s{2,}", tail) if p.strip()]
            child_indent = indent + 2
            log.debugx(
                "voice_utilities:normalize_mermaid_mindmap:split_root_tail",
                root_node=root_node,
                child_count=len(parts),
            )
            for p in parts:
                out.append(" " * child_indent + p)
            continue

        parts = [p.strip() for p in re.split(r"\s{2,}", text) if p.strip()]
        if len(parts) > 1:
            log.debugx(
                "voice_utilities:normalize_mermaid_mindmap:split_multi_part_line",
                indent=indent,
                part_count=len(parts),
            )
            out.append(" " * indent + parts[0])
            for p in parts[1:]:
                out.append(" " * indent + p)
        else:
            out.append(" " * indent + text)

    normalized: List[str] = []
    for ln in out:
        if ln.strip() == "mindmap":
            normalized.append("mindmap")
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        indent = (indent // 2) * 2
        normalized.append(" " * indent + ln.strip())

    result = "\n".join(normalized)
    log.infox(
        "voice_utilities:normalize_mermaid_mindmap:done",
        raw_line_count=len(raw_lines),
        output_line_count=len(normalized),
        result_len=len(result),
    )
    return result


# -----------------------------
# Simple JSON/JSONL IO helpers
# -----------------------------
def write_json(path: Path, obj: Any) -> None:
    log.debugx(
        "voice_utilities:write_json:start",
        path=str(path),
        object_type=type(obj).__name__,
        object_keys=list(obj.keys()) if isinstance(obj, dict) else None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debugx(
        "voice_utilities:write_json:done",
        path=str(path),
        exists=path.exists(),
        bytes_count=path.stat().st_size if path.exists() else 0,
    )


def read_json(path: Path) -> Any:
    log.debugx(
        "voice_utilities:read_json:start",
        path=str(path),
        exists=path.exists(),
    )
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        log.debugx(
            "voice_utilities:read_json:done",
            path=str(path),
            result_type=type(result).__name__,
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )
        return result
    except Exception:
        log.debugx(
            "voice_utilities:read_json:failed",
            path=str(path),
            exists=path.exists(),
        )
        return None


def write_text(path: Path, text: str) -> None:
    log.debugx(
        "voice_utilities:write_text:start",
        path=str(path),
        text_len=len(text or ""),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")
    log.debugx(
        "voice_utilities:write_text:done",
        path=str(path),
        exists=path.exists(),
        bytes_count=path.stat().st_size if path.exists() else 0,
    )


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    log.debugx(
        "voice_utilities:append_jsonl:start",
        path=str(path),
        object_keys=list(obj.keys()) if isinstance(obj, dict) else None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    log.debugx(
        "voice_utilities:append_jsonl:done",
        path=str(path),
        exists=path.exists(),
        bytes_count=path.stat().st_size if path.exists() else 0,
    )


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    log.debugx(
        "voice_utilities:read_jsonl:start",
        path=str(path),
        exists=path.exists(),
    )
    if not path.exists():
        log.debugx(
            "voice_utilities:read_jsonl:not_exists",
            path=str(path),
        )
        return []
    out: List[Dict[str, Any]] = []
    skipped = 0
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                out.append(obj)
            else:
                skipped += 1
        except Exception:
            skipped += 1
            continue
    log.debugx(
        "voice_utilities:read_jsonl:done",
        path=str(path),
        item_count=len(out),
        skipped_count=skipped,
    )
    return out


def atomic_write_json(path: Path, obj: Any) -> None:
    """
    Write JSON atomically via temp file + replace.
    """
    log.debugx(
        "voice_utilities:atomic_write_json:start",
        path=str(path),
        object_type=type(obj).__name__,
        object_keys=list(obj.keys()) if isinstance(obj, dict) else None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp.{path.name}.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.debugx(
        "voice_utilities:atomic_write_json:done",
        path=str(path),
        exists=path.exists(),
        bytes_count=path.stat().st_size if path.exists() else 0,
    )


def touch_marker(path: Path) -> None:
    log.debugx(
        "voice_utilities:touch_marker:start",
        path=str(path),
    )
    try:
        write_text(path, utc_iso())
        log.debugx(
            "voice_utilities:touch_marker:done",
            path=str(path),
            exists=path.exists(),
        )
    except Exception:
        log.debugx(
            "voice_utilities:touch_marker:failed",
            path=str(path),
        )


# -----------------------------
# ffmpeg / ffprobe helpers
# -----------------------------
def require_ffmpeg() -> str:
    log.debugx(
        "voice_utilities:require_ffmpeg:start",
        env_ffmpeg=bool(os.environ.get("FFMPEG_BIN")),
    )
    path = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not path:
        log.warningx("voice_utilities:require_ffmpeg:not_found")
        raise RuntimeError("ffmpeg not found. Install it or set FFMPEG_BIN.")
    log.debugx(
        "voice_utilities:require_ffmpeg:done",
        path=path,
    )
    return path


def require_ffprobe() -> str:
    log.debugx(
        "voice_utilities:require_ffprobe:start",
        env_ffprobe=bool(os.environ.get("FFPROBE_BIN")),
    )
    path = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
    if not path:
        log.warningx("voice_utilities:require_ffprobe:not_found")
        raise RuntimeError("ffprobe not found. Install it or set FFPROBE_BIN.")
    log.debugx(
        "voice_utilities:require_ffprobe:done",
        path=path,
    )
    return path


def run_cmd(cmd: List[str]) -> Tuple[int, str]:
    log.debugx(
        "voice_utilities:run_cmd:start",
        cmd=cmd,
    )
    p = subprocess.run(cmd, capture_output=True, text=True)
    stderr = (p.stderr or "")[:4000]
    log.debugx(
        "voice_utilities:run_cmd:done",
        returncode=p.returncode,
        stderr_len=len(stderr),
        stderr_preview=stderr[:300],
    )
    return p.returncode, stderr


def ffprobe_duration_s(path: Path) -> Optional[float]:
    log.infox(
        "voice_utilities:ffprobe_duration:start",
        path=str(path),
        exists=path.exists(),
        bytes_count=path.stat().st_size if path.exists() else None,
    )
    if not path.exists() or path.stat().st_size == 0:
        log.debugx(
            "voice_utilities:ffprobe_duration:missing_or_empty",
            path=str(path),
        )
        return None

    ffprobe_bin = require_ffprobe()
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    rc, _ = run_cmd(cmd)
    if rc != 0:
        log.warningx(
            "voice_utilities:ffprobe_duration:command_failed",
            path=str(path),
            rc=rc,
        )
        return None

    try:
        out = subprocess.check_output(cmd, text=True).strip()
        if not out:
            log.debugx(
                "voice_utilities:ffprobe_duration:empty_output",
                path=str(path),
            )
            return None
        d = float(out)
        result = d if d > 0 else None
        log.infox(
            "voice_utilities:ffprobe_duration:done",
            path=str(path),
            duration_s=result,
        )
        return result
    except Exception:
        log.warningx(
            "voice_utilities:ffprobe_duration:parse_failed",
            path=str(path),
        )
        return None


def webm_file_segment_to_wav_bytes(
    webm_path: Path,
    *,
    start_s: Optional[float] = None,
    dur_s: Optional[float] = None,
) -> Tuple[bytes, int, str]:
    """
    Convert a segment of a (growing) WebM recording into WAV.
    Accurate seek for WebM: place -ss AFTER -i.
    Returns (wav_bytes, ffmpeg_rc, ffmpeg_stderr_preview)
    """
    log.infox(
        "voice_utilities:webm_segment_to_wav:start",
        webm_path=str(webm_path),
        exists=webm_path.exists(),
        bytes_count=webm_path.stat().st_size if webm_path.exists() else None,
        start_s=start_s,
        dur_s=dur_s,
    )

    ffmpeg_bin = require_ffmpeg()

    if not webm_path.exists() or webm_path.stat().st_size == 0:
        log.debugx(
            "voice_utilities:webm_segment_to_wav:missing_or_empty",
            webm_path=str(webm_path),
        )
        return b"", 0, ""

    with tempfile.TemporaryDirectory() as d:
        out_path = Path(d) / "seg.wav"

        cmd: List[str] = [ffmpeg_bin, "-y", "-i", str(webm_path)]

        if start_s is not None:
            cmd += ["-ss", str(max(0.0, float(start_s)))]

        if dur_s is not None:
            cmd += ["-t", str(max(0.0, float(dur_s)))]

        cmd += ["-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_path)]

        log.debugx(
            "voice_utilities:webm_segment_to_wav:ffmpeg_cmd",
            webm_path=str(webm_path),
            cmd=cmd,
        )
        rc, stderr = run_cmd(cmd)
        if rc != 0 or not out_path.exists():
            log.warningx(
                "voice_utilities:webm_segment_to_wav:ffmpeg_failed",
                webm_path=str(webm_path),
                rc=rc,
                out_exists=out_path.exists(),
                stderr_preview=stderr[:300],
            )
            return b"", rc, stderr

        try:
            result = out_path.read_bytes()
            log.infox(
                "voice_utilities:webm_segment_to_wav:done",
                webm_path=str(webm_path),
                rc=rc,
                wav_bytes=len(result),
            )
            return result, rc, stderr
        except Exception:
            log.warningx(
                "voice_utilities:webm_segment_to_wav:read_failed",
                webm_path=str(webm_path),
                rc=rc,
            )
            return b"", rc, stderr


def wav_duration_s(wav_bytes: bytes) -> float:
    """
    Tiny WAV parser (PCM): returns duration seconds, else 0.0.
    """
    log.debugx(
        "voice_utilities:wav_duration:start",
        bytes_count=len(wav_bytes or b""),
    )
    try:
        if len(wav_bytes) < 44:
            log.debugx(
                "voice_utilities:wav_duration:too_short",
                bytes_count=len(wav_bytes or b""),
            )
            return 0.0
        if wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            log.debugx("voice_utilities:wav_duration:not_wav")
            return 0.0

        i = 12
        fmt = None
        data_size = None

        while i + 8 <= len(wav_bytes):
            chunk_id = wav_bytes[i:i + 4]
            chunk_size = int.from_bytes(wav_bytes[i + 4:i + 8], "little")
            chunk_start = i + 8
            chunk_end = chunk_start + chunk_size
            if chunk_end > len(wav_bytes):
                log.debugx(
                    "voice_utilities:wav_duration:chunk_out_of_bounds",
                    chunk_id=chunk_id.decode(errors="replace"),
                    chunk_size=chunk_size,
                )
                break

            if chunk_id == b"fmt " and chunk_size >= 16:
                num_channels = int.from_bytes(wav_bytes[chunk_start + 2:chunk_start + 4], "little")
                sample_rate = int.from_bytes(wav_bytes[chunk_start + 4:chunk_start + 8], "little")
                bits_per_sample = int.from_bytes(wav_bytes[chunk_start + 14:chunk_start + 16], "little")
                fmt = (num_channels, sample_rate, bits_per_sample)

                log.debugx(
                    "voice_utilities:wav_duration:fmt_chunk",
                    num_channels=num_channels,
                    sample_rate=sample_rate,
                    bits_per_sample=bits_per_sample,
                )

            if chunk_id == b"data":
                data_size = chunk_size
                log.debugx(
                    "voice_utilities:wav_duration:data_chunk",
                    data_size=data_size,
                )

            i = chunk_end + (chunk_size % 2)

        if not fmt or data_size is None:
            log.debugx(
                "voice_utilities:wav_duration:missing_fmt_or_data",
                has_fmt=fmt is not None,
                has_data=data_size is not None,
            )
            return 0.0

        num_channels, sample_rate, bits_per_sample = fmt
        if sample_rate <= 0 or num_channels <= 0 or bits_per_sample <= 0:
            log.debugx(
                "voice_utilities:wav_duration:invalid_fmt",
                num_channels=num_channels,
                sample_rate=sample_rate,
                bits_per_sample=bits_per_sample,
            )
            return 0.0

        bytes_per_sample = (bits_per_sample // 8) * num_channels
        if bytes_per_sample <= 0:
            log.debugx(
                "voice_utilities:wav_duration:invalid_bytes_per_sample",
                bytes_per_sample=bytes_per_sample,
            )
            return 0.0

        num_samples = data_size / bytes_per_sample
        result = float(num_samples / sample_rate)
        log.debugx(
            "voice_utilities:wav_duration:done",
            duration_s=result,
            data_size=data_size,
            num_samples=num_samples,
            sample_rate=sample_rate,
        )
        return result
    except Exception:
        log.debugx("voice_utilities:wav_duration:failed")
        return 0.0