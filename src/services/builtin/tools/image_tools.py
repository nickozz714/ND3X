"""
services/builtin/tools/image_tools.py

`image__view` — let the agent LOOK at an attached image again in a later hop.

The attachment pipeline describes images once at attach time; the agent loop
itself is text-based, so follow-up questions ("what's in the top-right
corner?") had nothing to look at. This tool re-opens the stored image bytes and
runs a targeted vision call (the active/planner model when vision-capable, else
any enabled vision-capable chat model) with the agent's specific question.

Registered on import — imported in ask_job_callbacks.py.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict

from component.config import settings
from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry
from services.builtin.tools.background_tasks import current_run_thread

log = get_logger(__name__)


def _thread_dir(thread_id: str) -> Path:
    # Same layout as the upload path — reuse ChatAttachmentService's resolution.
    from services.chat_attachment_service import ChatAttachmentService
    return ChatAttachmentService(Path(settings.ASK_JOB_ROOT)).thread_dir(thread_id)


def _downscaled(path: Path, media_type: str, *, max_px: int = 1280) -> tuple[bytes, str]:
    """Resize a large image before the vision call — models look at ~1K-px
    detail anyway, and this keeps request size and per-image cost bounded.
    Best-effort: on any failure the original bytes are used."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(path) as img:
            if max(img.size) <= max_px:
                return path.read_bytes(), media_type
            img.thumbnail((max_px, max_px))
            buf = BytesIO()
            if img.mode in ("RGBA", "P"):
                img.save(buf, format="PNG")
                return buf.getvalue(), "image/png"
            img.convert("RGB").save(buf, format="JPEG", quality=88)
            return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — resizing is an optimization, never a blocker
        return path.read_bytes(), media_type


def _find_image_record(thread_id: str, ref: str) -> Dict[str, Any] | None:
    """Find an image attachment by id or (case-insensitive) name for a thread."""
    ref_l = (ref or "").strip().lower()
    directory = _thread_dir(thread_id)
    if not directory.is_dir():
        return None
    for manifest in sorted(directory.glob("*.json")):
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — skip garbled manifests
            continue
        if not str(record.get("media_type") or "").startswith("image/"):
            continue
        if record.get("id") == ref or str(record.get("name") or "").lower() == ref_l:
            return record
    return None


GENERATED_IMAGES_DIRNAME = "generated_images"


def generated_images_dir() -> Path:
    d = Path(str(settings.FILES_DIR)) / GENERATED_IMAGES_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@internal_tool_registry.register(
    name="image__generate",
    title="Generate Image",
    description=(
        "Generate an image from a text prompt using the model on the "
        "image_generation routing slot. Returns a markdown image link to show the "
        "user, and registers the image as a thread attachment so image__view can "
        "inspect/verify it. Sizes: 1024x1024 (default), 1536x1024, 1024x1536."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to generate — be specific about subject, style and composition."},
            "size": {"type": "string", "description": "Optional: 1024x1024 (default), 1536x1024 or 1024x1536."},
        },
        "required": ["prompt"],
    },
    tags=["internal", "image", "generation"],
)
async def image_generate(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str((args or {}).get("prompt") or "").strip()
    if not prompt:
        return {"status": "error", "error": "image__generate requires a non-empty 'prompt'."}
    size = str((args or {}).get("size") or "1024x1024").strip()
    if size not in ("1024x1024", "1536x1024", "1024x1536"):
        size = "1024x1024"

    from db.database import SessionLocal
    from services.providers.image_generation import resolve_image_generation
    with SessionLocal() as db:
        resolved = resolve_image_generation(db)
    if resolved is None:
        return {
            "status": "error",
            "error": (
                "No model is assigned to the image_generation slot (AI Models → Routing). "
                "Assign an image model there first — e.g. OpenAI gpt-image-1, a Gemini "
                "image model, or an OpenAI-compatible /images/generations endpoint. "
                "Anthropic and Ollama cannot generate images."
            ),
        }
    provider, model_id = resolved
    try:
        png = await provider.generate(prompt, model=model_id, size=size)
    except Exception as exc:  # noqa: BLE001 — a failed generation is a tool error
        log.warningx("image__generate mislukt", model=model_id, error=str(exc))
        return {"status": "error", "error": f"Image generation failed on {model_id}: {exc}"}

    import time
    import uuid as _uuid
    filename = f"img_{time.strftime('%Y%m%dT%H%M%S')}_{_uuid.uuid4().hex[:8]}.png"
    out_path = generated_images_dir() / filename
    out_path.write_bytes(png)

    # Register as a thread attachment (same record shape as uploads), so
    # image__view can look at the result and retrieval keeps working.
    attachment_id = None
    thread_id = current_run_thread.get()
    if thread_id:
        try:
            thread_dir = _thread_dir(thread_id)
            thread_dir.mkdir(parents=True, exist_ok=True)
            attachment_id = _uuid.uuid4().hex
            att_path = thread_dir / f"{attachment_id}-{filename}"
            att_path.write_bytes(png)
            record = {
                "id": attachment_id,
                "name": filename,
                "media_type": "image/png",
                "size": len(png),
                "path": str(att_path),
                "generated": True,
                "prompt": prompt,
            }
            (thread_dir / f"{attachment_id}.json").write_text(json.dumps(record), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — registration is best-effort
            log.warningx("Gegenereerde afbeelding registreren als attachment mislukt", error=str(exc))
            attachment_id = None

    url = f"/api/images/file/{filename}"
    return {
        "status": "success",
        "model": model_id,
        "filename": filename,
        "url": url,
        "attachment_id": attachment_id,
        "markdown": f"![{prompt[:80]}]({url})",
        "note": "Include the markdown image in your final answer so the user sees it.",
    }


@internal_tool_registry.register(
    name="image__view",
    title="View Attached Image",
    description=(
        "Look at an image the user attached in this thread and answer a specific "
        "question about it (e.g. 'what does the error dialog say?'). Use when the "
        "existing image description doesn't cover what you need. Requires a "
        "vision-capable model to be available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Attachment name or id of the image to inspect."},
            "question": {"type": "string", "description": "What to look for / answer about the image."},
        },
        "required": ["image"],
    },
    tags=["internal", "image", "vision"],
)
async def image_view(args: Dict[str, Any]) -> Dict[str, Any]:
    ref = str((args or {}).get("image") or "").strip()
    question = str((args or {}).get("question") or "Describe this image accurately and compactly.").strip()
    if not ref:
        return {"status": "error", "error": "image__view requires 'image' (attachment name or id)."}

    thread_id = current_run_thread.get()
    if not thread_id:
        return {"status": "error", "error": "No active thread — image attachments are per-thread."}

    record = _find_image_record(thread_id, ref)
    if record is None:
        return {"status": "error", "error": f"No image attachment '{ref}' found in this thread."}

    path = Path(record.get("path") or "")
    if not path.is_file():
        return {"status": "error", "error": "The image file is no longer available."}

    from db.database import SessionLocal
    from services.providers.registry_service import ProviderRegistryService
    with SessionLocal() as db:
        vision_model = ProviderRegistryService(db).resolve_vision_model()
    if not vision_model:
        return {
            "status": "error",
            "error": (
                "No vision-capable chat model is enabled. Add one (gpt-5.x, claude, "
                "gemini, or a local llava/qwen-vl) or set a model's 'img' override in AI Models."
            ),
        }

    data, media_type = _downscaled(path, str(record.get("media_type") or "image/png"))
    encoded = base64.b64encode(data).decode("ascii")
    content = [
        {"type": "input_text", "text": question},
        {"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"},
    ]
    # The full LLM router resolves a registered vision model to its provider.
    from db.database import SessionLocal as _SL
    from services.assistants.ask_job_callbacks import openai as _openai_service
    from services.providers.provider_factory import build_llm_router
    with _SL() as db:
        llm = build_llm_router(_openai_service, db)
    try:
        result = await llm.ask_async(
            [{"role": "user", "content": content}],
            model=vision_model,
            max_output_tokens=1200,
            store=False,
        )
    except Exception as exc:  # noqa: BLE001 — a failed look is a tool error, not a crash
        log.warningx("image__view vision-call mislukt", model=vision_model, error=str(exc))
        return {"status": "error", "error": f"Vision call failed on {vision_model}: {exc}"}

    answer = (getattr(result, "text", "") or "").strip()
    return {
        "status": "success",
        "image": record.get("name"),
        "model": vision_model,
        "answer": answer or "(the vision model returned no text)",
    }
