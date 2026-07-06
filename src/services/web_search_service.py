"""Provider-native web search for the orchestrator.

Optional + routing-driven: enabled only when a model is assigned to the
`chat.web_search` routing slot. The search runs on THAT model's provider using its
native web search (OpenAI Responses web_search, Anthropic web_search tool, Gemini
google_search). Providers without native web search return a clear message telling
the user to add their own search MCP server instead. No third-party search key.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from component.logging import get_logger
from services.providers.registry_service import ProviderRegistryService

log = get_logger(__name__)


def search(db: Session, query: str, *, max_results: int = 5) -> Dict[str, Any]:
    """Run a web search via the agent's chat model, when that model supports native
    web search. Sync (call via asyncio.to_thread from async callers)."""
    if not (query or "").strip():
        return {"ok": False, "error": "query is required"}

    reg = ProviderRegistryService(db)
    # Web search runs on the agent's own model (chat.planner).
    resolved = reg.resolve_slot("chat.planner")
    if resolved is None:
        return {"ok": False, "error": "No chat model is configured — assign one under AI Models → Routing."}

    # Only proceed if this model actually supports native web search.
    from models.provider import ProviderModel
    from services.providers.web_search_capability import effective_web_search
    pm = (db.query(ProviderModel)
          .filter(ProviderModel.provider_id == resolved.provider_id, ProviderModel.model_id == resolved.model_id)
          .first())
    override = pm.supports_web_search if pm else None
    if not effective_web_search(resolved.provider_type, resolved.model_id, override):
        return {"ok": False, "error": (
            f"The active model ({resolved.provider_type}:{resolved.model_id}) doesn't support native web search. "
            "Use a web-search-capable model (OpenAI/Anthropic/Gemini) — set it to 'web: on' in AI Models if needed — "
            "or add your own search MCP server.")}

    key = reg.get_api_key(resolved.provider_id)
    if not key:
        return {"ok": False, "error": f"No API key configured for the web-search provider ({resolved.provider_type})."}

    ptype = (resolved.provider_type or "").lower()
    model = resolved.model_id
    try:
        if ptype in ("openai", "openai_compatible", "openai-compatible", "azure_openai"):
            return _openai(key, model, resolved.base_url, query)
        if ptype == "anthropic":
            return _anthropic(key, model, query, max_results)
        if ptype in ("gemini", "google", "google_genai"):
            return _gemini(key, model, query)
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the agent
        log.warningx("web search mislukt", provider=ptype, error=str(exc))
        return {"ok": False, "error": f"web search failed: {exc}"}
    return {"ok": False, "error": (
        f"Provider '{resolved.provider_type}' has no native web search. Assign an "
        f"OpenAI/Anthropic/Gemini model to chat.web_search, or add a search MCP server.")}


def _openai(key: str, model: str, base_url: str | None, query: str) -> Dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url or None)
    resp = client.responses.create(model=model, tools=[{"type": "web_search"}], input=query)
    return {"ok": True, "provider": "openai", "answer": getattr(resp, "output_text", "") or ""}


def _anthropic(key: str, model: str, query: str, max_results: int) -> Dict[str, Any]:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=1024,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_results}],
        messages=[{"role": "user", "content": query}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    return {"ok": True, "provider": "anthropic", "answer": text.strip()}


def _gemini(key: str, model: str, query: str) -> Dict[str, Any]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=model, contents=query,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    return {"ok": True, "provider": "gemini", "answer": getattr(resp, "text", "") or ""}
