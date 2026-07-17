"""Which models can LOOK AT images (vision / multimodal input).

Vision is a model property, not a provider-wide one. We use a curated default
per provider/model-family, overridable per model (`ProviderModel.supports_vision`)
in the AI Models UI. `effective_vision` is what the rest of the app should call.
Mirrors services/providers/web_search_capability.py.
"""
from __future__ import annotations

from typing import Optional

# Local model families with vision support (Ollama naming conventions).
_LOCAL_VISION_MARKERS = (
    "llava", "vision", "-vl", ":vl", "vl-", "qwen2.5vl", "qwen2-vl", "qwen3-vl",
    "gemma3", "moondream", "minicpm-v", "bakllava", "llama3.2-vision", "pixtral",
)


def provider_supports_vision(provider_type: Optional[str], model_id: Optional[str]) -> bool:
    """Curated default: can this provider+model accept image input?"""
    p = (provider_type or "").lower()
    m = (model_id or "").lower()
    if p in ("openai", "azure_openai"):
        # Embedding/audio/realtime models can't; flagship chat models can.
        if any(k in m for k in ("embedding", "whisper", "tts", "realtime", "audio")):
            return False
        return any(k in m for k in ("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4", "gpt-4-turbo"))
    if p == "azure_foundry":
        # Foundry hosts OpenAI models AND open models (Llama/Phi/Mistral/…).
        # model_id is the deployment name, which by convention contains the
        # model name — match both families. Per-model override wins as always.
        if any(k in m for k in ("embedding", "whisper", "tts", "realtime", "audio")):
            return False
        return (any(k in m for k in ("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4", "gpt-4-turbo"))
                or any(k in m for k in _LOCAL_VISION_MARKERS)
                or "multimodal" in m)
    if p == "anthropic":
        # All Claude 3+ chat models are vision-capable.
        return "claude" in m
    if p in ("gemini", "google", "google_genai"):
        return "gemini" in m and "embedding" not in m
    if p in ("ollama", "openai_compatible"):
        return any(k in m for k in _LOCAL_VISION_MARKERS)
    return False


def effective_vision(provider_type: Optional[str], model_id: Optional[str],
                     override: Optional[bool]) -> bool:
    """Per-model override wins; otherwise the curated default."""
    if override is not None:
        return bool(override)
    return provider_supports_vision(provider_type, model_id)
