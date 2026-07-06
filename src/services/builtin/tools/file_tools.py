from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Dict

from component.config import settings
from services.builtin.internal_tool_registry import internal_tool_registry
from services.builtin.tools.file_inspectors import (
    dispatch_inspect,
    inspect_archive,
    inspect_csv,
    inspect_json,
    inspect_notebook,
)


def _artifact_root() -> Path:
    return Path(settings.ASK_JOB_ROOT).resolve()


def _skill_files_root() -> Path:
    return (Path(settings.FILES_DIR) / "skills").resolve()


def _is_within_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_path(args: Dict[str, Any]) -> Path:
    content_ref = (args.get("content_ref") or "").strip()
    local_path = (args.get("local_path") or "").strip()
    artifact_root = _artifact_root()
    skill_root = _skill_files_root()

    if content_ref:
        m = re.match(r"^artifact://([^/]+)/([^/]+)/([^/]+)/(.+)$", content_ref)
        if not m:
            raise ValueError("Invalid content_ref format")
        thread_id, run_id, tool_call_id, filename = m.groups()
        candidate = artifact_root / thread_id / run_id / "artifacts" / tool_call_id / filename
    elif local_path:
        candidate = Path(local_path)
    else:
        raise ValueError("content_ref or local_path is required")

    resolved = candidate.resolve()
    if not (_is_within_root(resolved, artifact_root) or _is_within_root(resolved, skill_root)):
        raise ValueError("Path outside allowed file roots is not allowed")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("Artifact file not found")
    return resolved


@internal_tool_registry.register(
    name="file_preview",
    title="Artifact File Preview",
    description="Return safe preview and metadata for an artifact file.",
    input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "max_chars": {"type": "integer"}}},
    tags=["file", "internal"],
)
async def file_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    max_chars = int(args.get("max_chars") or settings.TOOL_RESULT_MAX_PREVIEW_CHARS)
    raw = p.read_text(encoding="utf-8", errors="replace")
    preview = raw[:max_chars]
    return {"status": "success", "mime_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream", "size_bytes": p.stat().st_size, "preview": preview, "truncated": len(raw) > max_chars}


@internal_tool_registry.register(name="file_read_text", title="Artifact File Read Text", description="Read text artifact up to safe max_chars.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "max_chars": {"type": "integer"}}}, tags=["file", "internal"])
async def file_read_text(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    max_chars = int(args.get("max_chars") or settings.TOOL_RESULT_MAX_INLINE_CHARS)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if len(raw) > max_chars:
        return {"status": "partial", "message": "File exceeds safe read limit; returning partial text.", "content_text": raw[:max_chars], "truncated": True}
    return {"status": "success", "content_text": raw, "truncated": False}


@internal_tool_registry.register(name="file_search_text", title="Artifact File Search Text", description="Search inside text artifact and return compact matches.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "query": {"type": "string"}, "max_matches": {"type": "integer"}, "context_chars": {"type": "integer"}}, "required": ["query"]}, tags=["file", "internal"])
async def file_search_text(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    query = (args.get("query") or "").strip()
    if not query:
        return {"status": "failed", "error": "query is required"}
    max_matches = int(args.get("max_matches") or 10)
    context_chars = int(args.get("context_chars") or 80)
    raw = p.read_text(encoding="utf-8", errors="replace")
    matches = []
    start = 0
    ql = len(query)
    while len(matches) < max_matches:
        idx = raw.lower().find(query.lower(), start)
        if idx < 0:
            break
        s = max(0, idx - context_chars)
        e = min(len(raw), idx + ql + context_chars)
        matches.append({"index": idx, "snippet": raw[s:e]})
        start = idx + ql
    return {"status": "success", "query": query, "match_count": len(matches), "matches": matches}


@internal_tool_registry.register(name="file_metadata", title="Artifact File Metadata", description="Return artifact metadata only.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}}}, tags=["file", "internal"])
async def file_metadata(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    ext = p.suffix.lower()
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    recommendation = "Use file_preview first, then file_search_text or file_read_text for focused inspection."
    return {"status": "success", "filename": p.name, "mime_type": mime, "size_bytes": p.stat().st_size, "extension": ext, "inspection_recommendation": recommendation}


@internal_tool_registry.register(name="file_inspect", title="Artifact File Inspect", description="Inspect artifact file and return compact type-specific facts.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "inspection_goal": {"type": "string"}, "max_chars": {"type": "integer"}, "max_rows": {"type": "integer"}, "max_cells": {"type": "integer"}}}, tags=["file", "internal"])
async def file_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    return dispatch_inspect(
        p,
        inspection_goal=(args.get("inspection_goal") or "").strip(),
        max_chars=int(args.get("max_chars") or settings.TOOL_RESULT_MAX_PREVIEW_CHARS),
        max_rows=int(args.get("max_rows") or 20),
        max_cells=int(args.get("max_cells") or 20),
    )


@internal_tool_registry.register(name="json_inspect", title="JSON Inspect", description="Inspect JSON artifact compactly.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "max_depth": {"type": "integer"}}}, tags=["file", "internal"])
async def json_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    return inspect_json(p, max_depth=int(args.get("max_depth") or 3), max_chars=settings.TOOL_RESULT_MAX_PREVIEW_CHARS)


@internal_tool_registry.register(name="csv_profile", title="CSV Profile", description="Profile CSV/TSV artifact compactly.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "sample_rows": {"type": "integer"}}}, tags=["file", "internal"])
async def csv_profile(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    return inspect_csv(p, max_rows=int(args.get("sample_rows") or 20))


@internal_tool_registry.register(name="notebook_inspect", title="Notebook Inspect", description="Inspect notebook artifact compactly.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "max_cells": {"type": "integer"}}}, tags=["file", "internal"])
async def notebook_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    return inspect_notebook(p, max_cells=int(args.get("max_cells") or 20))


@internal_tool_registry.register(name="archive_list", title="Archive List", description="List archive entries without extracting.", input_schema={"type": "object", "properties": {"content_ref": {"type": "string"}, "local_path": {"type": "string"}, "max_entries": {"type": "integer"}}}, tags=["file", "internal"])
async def archive_list(args: Dict[str, Any]) -> Dict[str, Any]:
    p = _resolve_path(args)
    return inspect_archive(p, max_entries=int(args.get("max_entries") or 50))
