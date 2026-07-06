"""
routers/pdf_routes.py
REST endpoints voor template beheer en PDF ophalen.
Registreer in routers/__init__.py.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter(prefix="/pdf", tags=["PDF"])


def _render_svc():
    from services.assistants.ask_job_callbacks import pdf_render_service
    return pdf_render_service


def _template_svc():
    from services.assistants.ask_job_callbacks import template_service
    return template_service


# ── Schemas ───────────────────────────────────────────────────────────────────

class RenderBody(BaseModel):
    markdown_string: str
    template: str = "beeminds"
    properties: Optional[Dict[str, Any]] = None


class UpdateTexBody(BaseModel):
    template_tex: str


class CreateTemplateBody(BaseModel):
    name: str
    copy_from: Optional[str] = None


# ── Render ────────────────────────────────────────────────────────────────────

@router.post("/render")
async def render_pdf(body: RenderBody) -> Response:
    """Render markdown naar PDF en stuur het bestand direct terug."""
    import asyncio
    from services.pdf.pdf_render_service import RenderRequest, validate_properties

    if body.properties:
        errors, _ = validate_properties(body.properties)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

    svc = _render_svc()
    req = RenderRequest(markdown=body.markdown_string, template=body.template, properties=body.properties)
    result = await asyncio.to_thread(svc.render, req)

    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )


@router.get("/file/{filename}")
def get_pdf_file(filename: str) -> Response:
    """Haal een eerder gegenereerde PDF op via bestandsnaam."""
    from services.assistants.ask_job_callbacks import pdf_render_service
    pdf_path = pdf_render_service.output_dir / filename
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF niet gevonden")
    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Templates — listing & detail ──────────────────────────────────────────────

@router.get("/templates")
def list_templates() -> List[Dict[str, Any]]:
    """Lijst alle beschikbare templates."""
    return _template_svc().list_templates()


@router.get("/templates/{name}")
def get_template(name: str) -> Dict[str, Any]:
    """Haal template details op inclusief .tex inhoud."""
    try:
        return _template_svc().get_template(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/templates")
def create_template(body: CreateTemplateBody) -> Dict[str, Any]:
    """Maak een nieuw template aan, optioneel gekopieerd van een bestaand template."""
    try:
        return _template_svc().create_template(body.name, copy_from=body.copy_from)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/templates/{name}")
def delete_template(name: str) -> Dict[str, Any]:
    """Verwijder een template inclusief alle assets."""
    try:
        _template_svc().delete_template(name)
        return {"deleted": True, "name": name}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Templates — tex bewerken ──────────────────────────────────────────────────

@router.put("/templates/{name}/tex")
def update_template_tex(name: str, body: UpdateTexBody) -> Dict[str, Any]:
    """Sla een bijgewerkt .tex bestand op voor een template."""
    try:
        return _template_svc().update_tex(name, body.template_tex)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Templates — assets ────────────────────────────────────────────────────────

@router.post("/templates/{name}/assets")
async def upload_asset(
    name: str,
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    """Upload een asset (logo, afbeelding) naar een template."""
    import base64
    data = await file.read()
    data_b64 = base64.b64encode(data).decode()
    try:
        return _template_svc().upload_asset(name, file.filename, data_b64)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/templates/{name}/assets/{filename}")
def get_asset(name: str, filename: str) -> Response:
    """Haal een template asset op als raw bytes."""
    import mimetypes
    try:
        data = _template_svc().get_asset(name, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(content=data, media_type=mime)


@router.delete("/templates/{name}/assets/{filename}")
def delete_asset(name: str, filename: str) -> Dict[str, Any]:
    """Verwijder een asset uit een template."""
    try:
        return _template_svc().delete_asset(name, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))