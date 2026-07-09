"""Which models support PROVIDER-NATIVE web search.

Web search is a provider + model-family feature, not arbitrary. We use a curated
default per provider/family, overridable per model (`ProviderModel.supports_web_search`)
in the AI Models UI. `effective_web_search` is what the rest of the app should call.
"""
from __future__ import annotations

from typing import Optional


def provider_supports_web_search(provider_type: Optional[str], model_id: Optional[str]) -> bool:
    """Curated default: does this provider+model offer native web search?"""
    p = (provider_type or "").lower()
    m = (model_id or "").lower()
    if p in ("openai", "azure_openai"):
        # OpenAI Responses `web_search` tool — current flagship / o-series models.
        return any(k in m for k in ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4", "gpt-4-turbo"))
    if p == "anthropic":
        # Anthropic `web_search` tool — Claude 3.5+/3.7/4.x.
        return any(k in m for k in ("claude-3-5", "claude-3.5", "claude-3-7", "claude-3.7",
                                    "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
                                    "claude-4", "opus-4", "sonnet-4", "haiku-4"))
    if p in ("gemini", "google", "google_genai"):
        # Gemini `google_search` grounding — 1.5 / 2.x.
        return any(k in m for k in ("gemini-1.5", "gemini-2", "gemini-exp"))
    if p == "claude_code":
        # Claude Code CLI ships its own WebSearch tool (subscription, all
        # models). Whether it's actually used is the provider's native_web
        # config choice, enforced in web_search_service; the per-model
        # "web: on/off" toggle here still wins as override.
        return True
    return False  # openai-compatible / local / unknown → no native web search


def effective_web_search(provider_type: Optional[str], model_id: Optional[str],
                         override: Optional[bool]) -> bool:
    """Per-model override wins; otherwise the curated default."""
    if override is not None:
        return bool(override)
    return provider_supports_web_search(provider_type, model_id)
