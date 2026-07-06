"""
services/builtin/tools/pdf_tools.py
Internal tool voor PDF rendering via de builtin MCP server.
"""
from __future__ import annotations
import asyncio
from typing import Any, Dict

from services.builtin.internal_tool_registry import internal_tool_registry


def _get_pdf_service():
    from services.assistants.ask_job_callbacks import pdf_render_service
    return pdf_render_service


@internal_tool_registry.register(
    name="pdf__render",
    title="Render Markdown to PDF",
    description=(
        "Render a PDF from a markdown string using a named template and optional document properties. "
        "Returns the filename and URI of the generated PDF."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "markdown_string": {"type": "string", "description": "The markdown content to render"},
            "template": {"type": "string", "description": "Template name (default: beeminds)"},
            "properties": {"type": "object", "description": "Optional YAML front matter properties (title, author, etc.)"},
        },
        "required": ["markdown_string"],
    },
    tags=["pdf", "internal"],
)
async def pdf_render(args: Dict[str, Any]) -> Dict[str, Any]:
    from services.pdf.pdf_render_service import RenderRequest, validate_properties

    markdown = args.get("markdown_string", "")
    template = args.get("template", "beeminds")
    properties = args.get("properties")

    if properties is not None:
        errors, warnings = validate_properties(properties)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}

    svc = _get_pdf_service()
    req = RenderRequest(markdown=markdown, template=template, properties=properties)
    result = await asyncio.to_thread(svc.render, req)

    return {
        "ok": True,
        "uri": f"pdf://{result.filename}",
        "filename": result.filename,
        "mime": "application/pdf",
        "pages": result.meta.get("pages"),
        "title": result.meta.get("title"),
        "warnings": result.warnings,
    }