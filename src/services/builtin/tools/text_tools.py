"""
services/builtin/tools/text_tools.py

Internal tools voor text ingestion, search, update en delete.
Worden geregistreerd bij import — zorg dat dit bestand geïmporteerd wordt
in ask_job_callbacks.py of server.py zodat de tools beschikbaar zijn.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)

# In-memory job queue voor async ingest
_INGEST_JOBS: Dict[str, Dict[str, Any]] = {}
_INGEST_LOCK = asyncio.Lock()
_ASYNC_THRESHOLD = 50_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_services():
    """Laadt de text services singletons lazy — vermijdt circular imports."""
    from services.assistants.ask_job_callbacks import text_indexer, text_searcher, text_storage
    return text_indexer, text_searcher, text_storage


# ── text__ingest ──────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__ingest",
    title="Ingest Text Content",
    description=(
        "Ingest text or code content into text storage and indexing. "
        "For large inputs (>50k chars) the job runs async — use text__ingest_status to poll."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content":                {"type": "string", "description": "The text or code content to ingest"},
            "title":                  {"type": "string", "description": "Optional title"},
            "subdir":                 {"type": "string", "description": "Subdirectory under files root (default: inbox)"},
            "source":                 {"type": "string", "description": "Source label (default: internal)"},
            "is_code":                {"type": "boolean", "description": "Set true for code content"},
            "language":               {"type": "string", "description": "Code language (python, javascript, etc.)"},
            "async_mode":             {"type": "boolean", "description": "Force async mode"},
            "async_threshold_chars":  {"type": "integer", "description": "Auto-async threshold (default 50000)"},
        },
        "required": ["content"],
    },
    tags=["text", "internal"],
)
async def text_ingest(args: Dict[str, Any]) -> Dict[str, Any]:
    from services.text.text_storage_service import IncomingText, IncomingCode

    content = args.get("content", "")
    if not content.strip():
        return {"status": "error", "error": "Empty content"}

    title = args.get("title")
    subdir = args.get("subdir", "inbox")
    source = args.get("source", "internal")
    is_code = bool(args.get("is_code", False))
    language = args.get("language", "python")
    threshold = int(args.get("async_threshold_chars", _ASYNC_THRESHOLD))
    async_mode = args.get("async_mode")
    if async_mode is None:
        async_mode = len(content) >= threshold

    item = (
        IncomingCode(source=source, title=title, content=content, subdir=subdir, language=language)
        if is_code
        else IncomingText(source=source, title=title, content=content, subdir=subdir)
    )

    if not async_mode:
        text_indexer, _, _ = _get_services()
        result = await asyncio.to_thread(text_indexer.ingest_text, item)
        return {"status": "done", "result": result}

    job_id = f"ing_{uuid.uuid4().hex}"
    job = {
        "job_id": job_id, "status": "queued",
        "created_at_ms": _now_ms(), "started_at_ms": None,
        "finished_at_ms": None, "error": None, "result": None,
        "meta": {"title": title, "subdir": subdir, "source": source,
                 "is_code": is_code, "content_chars": len(content)},
    }
    async with _INGEST_LOCK:
        _INGEST_JOBS[job_id] = job

    asyncio.create_task(_run_ingest_job(job_id, item))
    return {"status": "queued", "job_id": job_id}


async def _run_ingest_job(job_id: str, item) -> None:
    async with _INGEST_LOCK:
        if job_id in _INGEST_JOBS:
            _INGEST_JOBS[job_id].update({"status": "running", "started_at_ms": _now_ms()})
    try:
        text_indexer, _, _ = _get_services()
        result = await asyncio.to_thread(text_indexer.ingest_text, item)
        async with _INGEST_LOCK:
            if job_id in _INGEST_JOBS:
                _INGEST_JOBS[job_id].update({"status": "done", "finished_at_ms": _now_ms(), "result": result})
    except Exception as e:
        async with _INGEST_LOCK:
            if job_id in _INGEST_JOBS:
                _INGEST_JOBS[job_id].update({"status": "error", "finished_at_ms": _now_ms(), "error": f"{type(e).__name__}: {e}"})


# ── text__ingest_status ───────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__ingest_status",
    title="Get Ingest Status",
    description="Get the status of an async text ingest job by job ID.",
    input_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    tags=["text", "internal"],
)
async def text_ingest_status(args: Dict[str, Any]) -> Dict[str, Any]:
    job_id = args.get("job_id", "")
    async with _INGEST_LOCK:
        job = _INGEST_JOBS.get(job_id)
    if not job:
        return {"status": "error", "error": "Job not found", "job_id": job_id}
    return {k: job[k] for k in ("job_id", "status", "created_at_ms", "started_at_ms", "finished_at_ms", "error", "result", "meta")}


# ── text__ingest_wait ─────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__ingest_wait",
    title="Wait For Ingest",
    description="Poll an ingest job until it finishes or times out.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id":           {"type": "string"},
            "timeout_s":        {"type": "number", "description": "Timeout in seconds (default 30)"},
            "poll_interval_s":  {"type": "number", "description": "Poll interval in seconds (default 0.5)"},
        },
        "required": ["job_id"],
    },
    tags=["text", "internal"],
)
async def text_ingest_wait(args: Dict[str, Any]) -> Dict[str, Any]:
    job_id = args.get("job_id", "")
    timeout = float(args.get("timeout_s", 30.0))
    interval = float(args.get("poll_interval_s", 0.5))
    deadline = time.time() + timeout
    while True:
        status = await text_ingest_status({"job_id": job_id})
        if status.get("status") in ("done", "error"):
            return status
        if time.time() >= deadline:
            return {"status": "timeout", "job_id": job_id}
        await asyncio.sleep(interval)


# ── text__search ──────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__search",
    title="Search Text Index",
    description="Search indexed text content by semantic query.",
    input_schema={
        "type": "object",
        "properties": {
            "query":                {"type": "string"},
            "top_k":                {"type": "integer", "description": "Number of results (default 5)"},
            "include_file_content": {"type": "boolean", "description": "Include full file content (default true)"},
        },
        "required": ["query"],
    },
    tags=["text", "internal"],
)
async def text_search(args: Dict[str, Any]) -> Any:
    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    include_content = bool(args.get("include_file_content", True))

    _, text_searcher, _ = _get_services()
    hits = await asyncio.to_thread(text_searcher.search, query, top_k=top_k)

    results = []
    for h in hits:
        content = None
        if include_content:
            try:
                content = Path(h.file_path).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                content = None
        if "files/memory/threads" not in h.file_path:
            results.append({
                "score": h.score,
                "embedding_id": h.embedding_id,
                "doc_id": h.doc_id,
                "source": h.source,
                "file_path": h.file_path,
                "file_content": content,
            })
    return results


# ── text__delete ──────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__delete",
    title="Delete Text Document",
    description="Delete an indexed text document and optionally its backing file.",
    input_schema={
        "type": "object",
        "properties": {
            "doc_id":      {"type": "integer"},
            "delete_file": {"type": "boolean", "description": "Also delete the file on disk (default true)"},
        },
        "required": ["doc_id"],
    },
    tags=["text", "internal"],
)
async def text_delete(args: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = int(args.get("doc_id", 0))
    delete_file = bool(args.get("delete_file", True))
    text_indexer, _, _ = _get_services()
    return await asyncio.to_thread(text_indexer.delete_doc, doc_id=doc_id, delete_file=delete_file)


# ── text__update ──────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__update",
    title="Update Text Document",
    description="Update the content of an indexed text document.",
    input_schema={
        "type": "object",
        "properties": {
            "doc_id":      {"type": "integer"},
            "new_content": {"type": "string"},
        },
        "required": ["doc_id", "new_content"],
    },
    tags=["text", "internal"],
)
async def text_update(args: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = int(args.get("doc_id", 0))
    new_content = args.get("new_content", "")
    if not new_content.strip():
        return {"ok": False, "error": "Empty content"}
    text_indexer, _, _ = _get_services()
    res = await asyncio.to_thread(text_indexer.update_doc_content, doc_id=doc_id, new_content=new_content)
    if not res.get("ok", True):
        return res
    doc = await asyncio.to_thread(text_indexer.get_doc, doc_id=doc_id, include_content=True)
    return {"ok": True, "result": res, "doc": doc.get("doc")}


# ── text__list_files ──────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__list_files",
    title="List Text Files",
    description="List all files in text storage.",
    input_schema={"type": "object", "properties": {}},
    tags=["text", "internal"],
)
async def text_list_files(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    _, _, text_storage = _get_services()
    db = SessionLocal()
    try:
        files = await asyncio.to_thread(text_storage.list_files, db)
    finally:
        db.close()
    return {"ok": True, "count": len(files), "files": files}


# ── text__get_file ────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="text__get_file",
    title="Get Text File",
    description="Get the content of a stored text file by relative path.",
    input_schema={
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    },
    tags=["text", "internal"],
)
async def text_get_file(args: Dict[str, Any]) -> Dict[str, Any]:
    file_path = args.get("file_path", "")
    _, _, text_storage = _get_services()
    try:
        content = await asyncio.to_thread(text_storage.get_file, file_path)
        return {"ok": True, "path": file_path, "content": content}
    except FileNotFoundError:
        return {"ok": False, "error": "File not found", "path": file_path}