"""Tests for the Claude Code chat agent (option A): runs a chat turn as an
autonomous agent with ND3X tools via the gateway, and the router helper that
detects when the planner slot is claude_code."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Full model registry so Skill's relationship strings (SkillTool, …) resolve.
for _m in (
    "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
    "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
    "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
    "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
    "transfer", "meeting_profile", "slash_command", "secret", "board", "workflow",
):
    __import__(f"models.{_m}")

import models.provider as pv
import models.skill as sk
import services.assistants.claude_code_chat_agent as cca
from db.database import Base
from services.providers.base import ChatResult
from services.assistants.claude_code_chat_agent import ClaudeCodeChatAgent


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _add_skill(db, name, instructions):
    from datetime import datetime
    row = sk.Skill(name=name, description=f"{name} desc", instructions=instructions,
                   created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(row); db.commit()
    return row


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


def test_run_includes_selected_skill_instructions(monkeypatch, db):
    _add_cc(db)
    _add_skill(db, "fabric_query", "Always call fabric_list_workspaces before answering.")
    _add_skill(db, "unused", "Should not appear.")
    capture: dict = {}

    async def fake_chat(self, user_input, **kwargs):
        capture["instructions"] = kwargs.get("instructions")
        return ChatResult(text="ok", provider="claude_code", model="opus")

    import services.providers.claude_code_provider as ccp
    monkeypatch.setattr(ccp.ClaudeCodeChatProvider, "chat", fake_chat)
    monkeypatch.setattr(ClaudeCodeChatAgent, "_write_gateway_config", staticmethod(lambda: None))

    asyncio.run(ClaudeCodeChatAgent(db).run(user_input="q", skill_names=["fabric_query"]))
    instr = capture["instructions"]
    assert "fabric_query" in instr and "fabric_list_workspaces" in instr
    assert "Should not appear" not in instr  # only selected skills


def test_run_stream_yields_deltas(monkeypatch, db):
    _add_cc(db)

    async def fake_stream(self, user_input, **kwargs):
        for chunk in ("Hallo ", "wereld"):
            yield chunk

    import services.providers.claude_code_provider as ccp
    monkeypatch.setattr(ccp.ClaudeCodeChatProvider, "chat_stream", fake_stream)
    monkeypatch.setattr(ClaudeCodeChatAgent, "_write_gateway_config", staticmethod(lambda: None))

    async def collect():
        return [d async for d in ClaudeCodeChatAgent(db).run_stream(user_input="q")]

    assert asyncio.run(collect()) == ["Hallo ", "wereld"]


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
