"""
services/providers/image_generation.py

Provider-agnostic image GENERATION (TODO 8) — the counterpart of vision
(looking). One interface, an adapter per provider type that can generate:

- openai:             Images API (gpt-image-1 / dall-e-3)
- gemini:             generateContent with IMAGE response modality
- openai_compatible:  /v1/images/generations — the universal adapter for
                      LocalAI, Together (FLUX), Fireworks and local
                      ComfyUI/SD-WebUI bridges

Anthropic and Ollama cannot generate images at all — no adapter exists, so an
image_generation slot can never be pointed at them (visible, not silently
broken). The model comes from the optional ``image_generation`` routing slot:
unassigned = the feature is off (same explicit rule as tts/transcription).
"""
from __future__ import annotations

import abc
import base64
from typing import Any, Optional, Tuple

import httpx

from component.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


class ImageGenerationProvider(abc.ABC):
    provider_type: str = "base"

    @abc.abstractmethod
    async def generate(
        self, prompt: str, *, model: Optional[str] = None, size: str = "1024x1024",
    ) -> bytes:
        """Generate one image and return the PNG bytes."""
        raise NotImplementedError


class OpenAIImageGenerationProvider(ImageGenerationProvider):
    """OpenAI Images API — also serves any OpenAI-compatible /v1 endpoint that
    implements /images/generations (LocalAI, Together, SD-WebUI bridges), which
    is why base_url is configurable."""

    provider_type = "openai"

    def __init__(self, *, api_key: Optional[str], base_url: Optional[str] = None, default_model: str = ""):
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._default_model = default_model

    async def generate(self, prompt: str, *, model: Optional[str] = None, size: str = "1024x1024") -> bytes:
        body: dict[str, Any] = {
            "model": model or self._default_model,
            "prompt": prompt,
            "size": size,
            "n": 1,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{self._base_url}/images/generations", json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(_body_error(resp))
        data = resp.json()
        items = data.get("data") or []
        if not items:
            raise RuntimeError("The image API returned no image data.")
        item = items[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        url = item.get("url")
        if url:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                img = await client.get(url)
            img.raise_for_status()
            return img.content
        raise RuntimeError("The image API returned neither b64_json nor url.")


class OpenAICompatibleImageGenerationProvider(OpenAIImageGenerationProvider):
    provider_type = "openai_compatible"


class GeminiImageGenerationProvider(ImageGenerationProvider):
    """Gemini image generation via generateContent with an IMAGE response
    modality (gemini-*-image models / Imagen-backed)."""

    provider_type = "gemini"

    def __init__(self, *, api_key: str, default_model: str = ""):
        self._api_key = api_key
        self._default_model = default_model

    async def generate(self, prompt: str, *, model: Optional[str] = None, size: str = "1024x1024") -> bytes:
        model_id = model or self._default_model
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_id}:generateContent?key={self._api_key}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(_body_error(resp))
        data = resp.json()
        for candidate in data.get("candidates") or []:
            for part in ((candidate.get("content") or {}).get("parts")) or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise RuntimeError("Gemini returned no image data for this prompt/model.")


def _body_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if err:
                return str(err)
    except Exception:  # noqa: BLE001
        pass
    return (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"


def resolve_image_generation(db) -> Optional[Tuple[ImageGenerationProvider, str]]:
    """(provider, model_id) for the ``image_generation`` slot, or None when the
    slot is unassigned / the provider type cannot generate images."""
    from services.providers.registry_service import ProviderRegistryService

    reg = ProviderRegistryService(db)
    resolved = reg.resolve_slot("image_generation")
    if resolved is None:
        return None
    p = reg.get_provider(resolved.provider_id)
    if p is None:
        return None
    api_key = reg.get_api_key(p.id)
    ptype = (p.provider_type or "").strip()
    if ptype == "openai":
        return OpenAIImageGenerationProvider(api_key=api_key, default_model=resolved.model_id), resolved.model_id
    if ptype == "openai_compatible":
        return (
            OpenAICompatibleImageGenerationProvider(
                api_key=api_key, base_url=p.base_url, default_model=resolved.model_id,
            ),
            resolved.model_id,
        )
    if ptype == "gemini" and api_key:
        return GeminiImageGenerationProvider(api_key=api_key, default_model=resolved.model_id), resolved.model_id
    log.warningx("Provider-type kan geen afbeeldingen genereren", provider_type=ptype)
    return None
