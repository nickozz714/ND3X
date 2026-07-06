"""Unit tests for provider health checks + fallback chat wrapper (Phase 5)."""
from __future__ import annotations

import asyncio

import httpx

from services.providers.base import ChatProvider, ChatResult
from services.providers.health_service import FallbackChatProvider, check_provider


def test_check_keyed_providers():
    assert asyncio.run(check_provider(provider_type="anthropic", base_url=None, has_api_key=True))["status"] == "ok"
    assert asyncio.run(check_provider(provider_type="anthropic", base_url=None, has_api_key=False))["status"] == "unconfigured"
    assert asyncio.run(check_provider(provider_type="weird", base_url=None, has_api_key=True))["status"] == "unknown"


def test_check_compatible_reachable_and_unreachable():
    def ok_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    def err_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    ok_client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(err_handler))

    r_ok = asyncio.run(check_provider(provider_type="ollama", base_url="http://localhost:11434/v1", has_api_key=False, client=ok_client))
    assert r_ok["status"] == "ok"
    r_bad = asyncio.run(check_provider(provider_type="openai_compatible", base_url="http://localhost:9/v1", has_api_key=False, client=bad_client))
    assert r_bad["status"] == "unreachable"

    # missing base_url
    assert asyncio.run(check_provider(provider_type="ollama", base_url=None, has_api_key=False))["status"] == "unconfigured"


# ── fallback wrapper ──────────────────────────────────────────────────────────
class _GoodChat(ChatProvider):
    provider_type = "cloud"

    async def chat(self, user_input, **kwargs):
        return ChatResult(text="cloud-reply", provider="cloud")


class _FailingChat(ChatProvider):
    provider_type = "local"

    async def chat(self, user_input, **kwargs):
        raise RuntimeError("local model down")


def test_fallback_used_on_primary_failure():
    wrapped = FallbackChatProvider(_FailingChat(), _GoodChat())
    res = asyncio.run(wrapped.chat("hi", model="qwen2.5"))
    assert res.text == "cloud-reply"
    assert res.usage.get("fallback_used") is True


def test_fallback_not_used_when_primary_ok():
    wrapped = FallbackChatProvider(_GoodChat(), _FailingChat())
    res = asyncio.run(wrapped.chat("hi"))
    assert res.text == "cloud-reply"
    assert "fallback_used" not in (res.usage or {})
