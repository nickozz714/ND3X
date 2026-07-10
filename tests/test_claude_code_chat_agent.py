"""Tests for the Claude Code chat agent (option A): runs a chat turn as an
autonomous agent with ND3X tools via the gateway, and the router helper that
detects when the planner slot is claude_code."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
import services.assistants.claude_code_chat_agent as cca
from services.providers.base import ChatResult
from services.assistants.claude_code_chat_agent import ClaudeCodeChatAgent


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for m in (pv.Provider, pv.ProviderModel, pv.CapabilityAssignment):
        m.__table__.create(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _add_cc(db, config=None):
    p = pv.Provider(name="CC", provider_type="claude_code", enabled=True,
                    config_json=__import__("json").dumps(config) if config else None)
    db.add(p); db.commit()
    return p


def test_available(db):
    assert ClaudeCodeChatAgent(db).available() is False
    _add_cc(db)
    assert ClaudeCodeChatAgent(db).available() is True


def test_run_is_agentic_with_gateway(monkeypatch, db):
    _add_cc(db)
    capture: dict = {}

    async def fake_chat(self, user_input, **kwargs):
        capture["agentic"] = self._agentic
        capture["extra_args"] = list(self._extra_args)
        capture["instructions"] = kwargs.get("instructions")
        return ChatResult(text="Hier is je antwoord.", provider="claude_code", model="opus")

    import services.providers.claude_code_provider as ccp
    monkeypatch.setattr(ccp.ClaudeCodeChatProvider, "chat", fake_chat)
    monkeypatch.setattr(ClaudeCodeChatAgent, "_write_gateway_config",
                        staticmethod(lambda: "/tmp/fake-chat-mcp.json"))
    monkeypatch.setattr(cca.os, "unlink", lambda p: None)

    answer = asyncio.run(ClaudeCodeChatAgent(db).run(user_input="lijst mijn Fabric workspaces"))
    assert answer == "Hier is je antwoord."
    # Full agent, with the ND3X gateway attached and the agent instruction.
    assert capture["agentic"] is True
    assert "--mcp-config" in capture["extra_args"]
    assert "/tmp/fake-chat-mcp.json" in capture["extra_args"]
    assert "mcp__nd3x" in capture["instructions"]


def test_run_without_provider_raises(db):
    with pytest.raises(RuntimeError, match="No enabled Claude Code provider"):
        asyncio.run(ClaudeCodeChatAgent(db).run(user_input="x"))


def test_router_chat_provider_type():
    from services.providers.llm_router import LLMRouter

    class _P:
        provider_type = "claude_code"

    # Resolver returns (provider, eff_model) like the real one.
    r = LLMRouter(None, resolve_chat_provider=lambda model, role: (_P(), "opus"))
    assert r.chat_provider_type(None, "chat.planner") == "claude_code"
    # None resolution → OpenAI base path.
    r2 = LLMRouter(None, resolve_chat_provider=lambda model, role: None)
    assert r2.chat_provider_type(None, "chat.planner") is None
