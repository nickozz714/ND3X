"""
services/providers/health_service.py

Provider health checks + a fallback chat wrapper (Phase 5).

- check_provider: lightweight reachability/config check per provider type.
- FallbackChatProvider: try a primary chat provider, fall back to a secondary on
  failure (e.g. local model down -> cloud).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from component.logging import get_logger
from services.providers.base import ChatInput, ChatProvider, ChatResult

log = get_logger(__name__)


async def check_provider(
    *,
    provider_type: str,
    base_url: Optional[str],
    has_api_key: bool,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Return {status, detail}. status ∈ ok | unconfigured | unreachable | unknown."""
    t = (provider_type or "").strip()

    if t in ("openai_compatible", "ollama"):
        if not base_url:
            return {"status": "unconfigured", "detail": "missing base_url"}
        # Ollama exposes /api/version; OpenAI-compatible exposes /models.
        root = base_url.rstrip("/")
        probe = root[:-3] + "/api/version" if root.endswith("/v1") else root + "/models"
        try:
            if client is not None:
                r = await client.get(probe, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get(probe)
            return {"status": "ok" if r.status_code < 400 else "unreachable", "detail": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "detail": str(exc)}

    if t in ("anthropic", "openai", "gemini", "voyage"):
        return {"status": "ok" if has_api_key else "unconfigured",
                "detail": "API key present" if has_api_key else "no API key"}

    if t == "claude_code":
        # Local CLI — healthy when the binary is on PATH; auth (setup-token or
        # host login) only surfaces at call time. A custom cli_path lives in
        # config_json, which this probe doesn't see; PATH covers the default.
        import shutil
        found = shutil.which("claude")
        if found:
            return {"status": "ok", "detail": f"claude CLI at {found}"}
        return {"status": "unconfigured", "detail": "claude CLI not found on PATH"}

    return {"status": "unknown", "detail": f"unhandled provider type {t!r}"}


class FallbackChatProvider(ChatProvider):
    """Wraps a primary provider; on any exception, retries on the fallback."""

    def __init__(self, primary: ChatProvider, fallback: ChatProvider):
        self._primary = primary
        self._fallback = fallback
        self.provider_type = getattr(primary, "provider_type", "fallback")

    async def chat(self, user_input: ChatInput, **kwargs: Any) -> ChatResult:
        try:
            return await self._primary.chat(user_input, **kwargs)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback provider
            log.warningx("Primaire provider faalde; fallback gebruikt",
                         primary=getattr(self._primary, "provider_type", "?"), error=str(exc))
            res = await self._fallback.chat(user_input, **kwargs)
            if isinstance(res, ChatResult):
                res.usage = {**(res.usage or {}), "fallback_used": True}
            return res
