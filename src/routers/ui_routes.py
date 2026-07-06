from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi import Response

from authentication.dependencies import require_user
from models.ui_models import (
    IngestBody, IngestStatusBody, TextUpdateRequest,
    TextDeleteRequest, MarkdownToPdfRequest,
)
from services.audit_service import AuditService
from services.assistants.ask_job_callbacks import text_indexer, text_storage
from db.database import SessionLocal

router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(require_user)])
audit = AuditService()


# ── Text — direct via services ────────────────────────────────────────────────

@router.post("/ui/ingest")
async def ui_ingest(body: IngestBody) -> Dict[str, Any]:
    import asyncio
    import uuid
    from services.text.text_storage_service import IncomingText, IncomingCode

    content = body.content or ""
    if not content.strip():
        return {"status": "error", "error": "Empty content"}

    item = (
        IncomingCode(
            source="ui",
            title=body.title,
            content=content,
            subdir=body.subdir or "inbox",
            language=body.language or "python",
        )
        if body.is_code
        else IncomingText(
            source="ui",
            title=body.title,
            content=content,
            subdir=body.subdir or "inbox",
        )
    )

    threshold = 50_000
    async_mode = body.async_mode if body.async_mode is not None else len(content) >= threshold

    if not async_mode:
        result = await asyncio.to_thread(text_indexer.ingest_text, item)
        return {"status": "done", "result": result}

    # Async via internal tool registry (deelt job queue met text_tools)
    from services.builtin.tools.text_tools import _INGEST_JOBS, _INGEST_LOCK, _run_ingest_job, _now_ms
    job_id = f"ing_{uuid.uuid4().hex}"
    job = {
        "job_id": job_id, "status": "queued",
        "created_at_ms": _now_ms(), "started_at_ms": None,
        "finished_at_ms": None, "error": None, "result": None,
        "meta": {"title": body.title, "subdir": body.subdir, "source": "ui",
                 "is_code": bool(body.is_code), "content_chars": len(content)},
    }
    async with _INGEST_LOCK:
        _INGEST_JOBS[job_id] = job
    asyncio.create_task(_run_ingest_job(job_id, item))
    return {"status": "queued", "job_id": job_id}


@router.post("/ui/ingest_status")
async def ui_ingest_status(body: IngestStatusBody) -> Dict[str, Any]:
    from services.builtin.tools.text_tools import _INGEST_JOBS, _INGEST_LOCK
    async with _INGEST_LOCK:
        job = _INGEST_JOBS.get(body.job_id)
    if not job:
        return {"status": "error", "error": "Job not found", "job_id": body.job_id}
    return {k: job[k] for k in ("job_id", "status", "created_at_ms", "started_at_ms", "finished_at_ms", "error", "result", "meta")}


@router.post("/text_update")
async def text_update(req: TextUpdateRequest) -> Dict[str, Any]:
    import asyncio
    if not (req.new_content or "").strip():
        return {"ok": False, "error": "Empty content"}
    res = await asyncio.to_thread(text_indexer.update_doc_content, doc_id=int(req.doc_id), new_content=req.new_content)
    if not res.get("ok", True):
        return res
    doc = await asyncio.to_thread(text_indexer.get_doc, doc_id=int(req.doc_id), include_content=True)
    return {"ok": True, "result": res, "doc": doc.get("doc")}


@router.post("/text_delete")
async def text_delete(req: TextDeleteRequest) -> Dict[str, Any]:
    import asyncio
    return await asyncio.to_thread(text_indexer.delete_doc, doc_id=int(req.doc_id), delete_file=bool(req.delete_file))


@router.get("/text/files")
async def ui_text_files() -> Dict[str, Any]:
    import asyncio
    db = SessionLocal()
    try:
        files = await asyncio.to_thread(text_storage.list_files, db)
    finally:
        db.close()
    return {"ok": True, "count": len(files), "files": files}


@router.get("/text/file")
async def ui_text_file(path: str = Query(..., description="Relative path from storage root")) -> Dict[str, Any]:
    import asyncio
    try:
        content = await asyncio.to_thread(text_storage.get_file, path)
        return {"ok": True, "path": path, "content": content}
    except FileNotFoundError:
        return {"ok": False, "error": "File not found", "path": path}

# ── Audit ─────────────────────────────────────────────────────────────────────

@router.get("/audit/search")
async def audit_search(
    q: Optional[str] = Query(None),
    thread_id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    total, items = audit.search(q=q, thread_id=thread_id, type=type, level=level, limit=limit, offset=offset)
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/audit/threads")
async def audit_threads(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    from sqlalchemy import select, func
    from db.database import SessionLocal
    from models.audit import AuditTraceEvent

    with SessionLocal() as db:
        subq = (
            select(
                AuditTraceEvent.thread_id.label("thread_id"),
                func.min(AuditTraceEvent.ts).label("first_ts"),
                func.max(AuditTraceEvent.ts).label("last_ts"),
                func.count().label("count"),
            )
            .group_by(AuditTraceEvent.thread_id)
            .order_by(func.max(AuditTraceEvent.ts).desc())
            .limit(limit)
            .offset(offset)
            .subquery()
        )
        rows = db.execute(select(subq)).all()

    items = [{"thread_id": r.thread_id, "first_ts": r.first_ts, "last_ts": r.last_ts, "count": r.count} for r in rows]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/audit/thread/{thread_id}")
async def audit_thread(
    thread_id: str,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    total, items = audit.get_thread_events(thread_id=thread_id, limit=limit, offset=offset)
    return {"thread_id": thread_id, "total": total, "items": items, "limit": limit, "offset": offset}

@router.post("/pdf/render")
async def ui_markdown_to_pdf(body: MarkdownToPdfRequest) -> Dict[str, Any]:
    import asyncio
    from services.pdf.pdf_render_service import RenderRequest, validate_properties
    from services.assistants.ask_job_callbacks import pdf_render_service

    if body.properties:
        errors, warnings = validate_properties(body.properties)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors, "warnings": warnings})

    req = RenderRequest(
        markdown=body.markdown_string,
        template=body.template or "beeminds",
        properties=body.properties,
    )
    result = await asyncio.to_thread(pdf_render_service.render, req)
    return {
        "uri": f"pdf://{result.filename}",
        "mime": "application/pdf",
        "filename": result.filename,
        "warnings": result.warnings,
    }


@router.get("/pdf/{doc_id}")
async def ui_get_pdf(doc_id: str) -> Response:
    from services.assistants.ask_job_callbacks import pdf_render_service
    # doc_id is de volledige bestandsnaam
    pdf_path = pdf_render_service.output_dir / doc_id
    if not pdf_path.exists():
        # Fallback: zoek op prefix
        matches = list(pdf_render_service.output_dir.glob(f"{doc_id}*.pdf"))
        if not matches:
            raise HTTPException(status_code=404, detail="not found")
        pdf_path = matches[0]
    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf_path.name}"'},
    )


@router.get("/pdf/file/{file_name}")
async def ui_get_pdf_file(file_name: str) -> Response:
    from services.assistants.ask_job_callbacks import pdf_render_service
    pdf_path = pdf_render_service.output_dir / file_name
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )