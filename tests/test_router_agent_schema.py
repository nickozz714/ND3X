"""Fase 4 — decision/generator slots on a CLI-agent provider: ask_orchestration_async
puts the json_schema IN the prompt (a CLI agent can't enforce response_format), and
does NOT force response_format. A plain structured provider keeps the old behavior."""
from __future__ import annotations

import asyncio

from services.providers.base import ChatResult
from services.providers.llm_router import LLMRouter

_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class _Provider:
    def __init__(self, *, is_cli_agent, supports_structured_output):
        self.provider_type = "claude_code" if is_cli_agent else "ollama"
        self.is_cli_agent = is_cli_agent
        self.supports_structured_output = supports_structured_output
        self.captured: dict = {}

    async def chat(self, user_input, **kwargs):
        self.captured["instructions"] = kwargs.get("instructions")
        self.captured["response_format"] = kwargs.get("response_format")
        return ChatResult(text='{"ok": true}', provider=self.provider_type, model="m")


def _router(provider):
    return LLMRouter(None, resolve_chat_provider=lambda model, role: (provider, "m"))


def test_cli_agent_gets_schema_in_prompt_not_response_format():
    p = _Provider(is_cli_agent=True, supports_structured_output=False)
    asyncio.run(_router(p).ask_orchestration_async(
        "decide", role="memory_decision:planner", json_schema=_SCHEMA, instructions="Base."))
    assert "JSON object matching this schema" in p.captured["instructions"]
    assert '"ok"' in p.captured["instructions"]
    assert p.captured["response_format"] is None  # a CLI agent can't enforce it


def test_structured_provider_still_uses_response_format():
    p = _Provider(is_cli_agent=False, supports_structured_output=True)
    asyncio.run(_router(p).ask_orchestration_async(
        "decide", role="memory_decision:planner", json_schema=_SCHEMA, instructions="Base."))
    # Schema goes via response_format, NOT injected into the prompt.
    assert p.captured["response_format"]["type"] == "json_schema"
    assert "JSON object matching this schema" not in (p.captured["instructions"] or "")


def test_writer_role_is_prose_for_cli_agent():
    p = _Provider(is_cli_agent=True, supports_structured_output=False)
    asyncio.run(_router(p).ask_orchestration_async(
        "write it", role="writer:final", json_schema=_SCHEMA, instructions="Base."))
    # Writer stays prose — no schema directive appended.
    assert "JSON object matching this schema" not in (p.captured["instructions"] or "")
