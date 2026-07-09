"""Unit tests for the Claude Code workflow execution engine.

No CLI: the provider's chat() is stubbed. Verifies the runner returns a
pipeline-shaped result (so the executor's handoff gate / For-Each / follow-ups
apply unchanged) and that the JSON envelope from the autonomous run is parsed
into answer + downstream_handoff — including the tolerant fallback.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.providers.base import ChatResult
from services.workflows.claude_code_operation_runner import ClaudeCodeOperationRunner


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


def _add_provider(db, *, enabled=True, config=None):
    p = pv.Provider(name="CC", provider_type="claude_code", enabled=enabled,
                    config_json=json.dumps(config) if config else None)
    db.add(p); db.commit()
    return p


def _stub_chat(monkeypatch, text: str, capture: dict | None = None):
    import services.providers.claude_code_provider as ccp

    async def fake_chat(self, user_input, **kwargs):
        if capture is not None:
            capture["prompt"] = user_input
            capture["instructions"] = kwargs.get("instructions")
            capture["agentic"] = self._agentic
            capture["allowed_tools"] = self._allowed_tools
            capture["max_turns"] = self._max_turns
        return ChatResult(text=text, response_id="sess-9", provider="claude_code",
                          model="opus", usage={"output_tokens": 5})

    monkeypatch.setattr(ccp.ClaudeCodeChatProvider, "chat", fake_chat)


def test_provider_available(db):
    runner = ClaudeCodeOperationRunner(db)
    assert runner.provider_available() is False
    _add_provider(db)
    assert runner.provider_available() is True
    # Disabled provider doesn't count.
    db.query(pv.Provider).update({pv.Provider.enabled: False})
    db.commit()
    assert runner.provider_available() is False


def test_run_parses_json_envelope(monkeypatch, db):
    _add_provider(db, config={"native_bash": True})
    envelope = {
        "answer": "Wrote report.md",
        "downstream_handoff": {
            "summary": "Report generated",
            "status": "success",
            "artifacts": [{"path": "report.md", "status": "created"}],
            "iterables": {},
        },
    }
    capture: dict = {}
    _stub_chat(monkeypatch, "Done.\n\n" + json.dumps(envelope), capture)

    runner = ClaudeCodeOperationRunner(db)
    out = asyncio.run(runner.run(
        question="Generate the report",
        operation_config={"execution": {"engine": "claude_code",
                                        "allowed_tools": "Bash Read", "max_turns": 12}},
        run_transcript=[{"role": "assistant", "content": "gathered data"}],
        operation_id=7, workflow_run_id=3,
    ))
    # Pipeline-shaped result.
    assert out["mode"] == "final"
    assert out["answer"] == "Wrote report.md"
    assert out["downstream_handoff"]["status"] == "success"
    assert out["downstream_handoff"]["artifacts"][0]["path"] == "report.md"
    assert out["engine"] == "claude_code"
    assert out["terminal_state"] == "completed"
    # Runs as an autonomous (agentic) task with the per-step tool allowlist.
    assert capture["agentic"] is True
    assert capture["allowed_tools"] == "Bash Read"
    assert capture["max_turns"] == 12
    # Handoff instruction + prior-step transcript reach the CLI.
    assert "downstream_handoff" in capture["instructions"]
    assert "gathered data" in capture["prompt"]
    assert "Generate the report" in capture["prompt"]


def test_run_tolerates_prose_only_result(monkeypatch, db):
    # An autonomous run that answered in prose (no JSON) must not hard-fail:
    # wrap it as a success handoff so the workflow continues.
    _add_provider(db)
    _stub_chat(monkeypatch, "The weather tomorrow in Urmond is sunny, ~22°C.")
    runner = ClaudeCodeOperationRunner(db)
    out = asyncio.run(runner.run(question="weer", operation_config={}))
    assert out["answer"].startswith("The weather")
    assert out["downstream_handoff"]["status"] == "success"
    assert "Urmond" in out["downstream_handoff"]["summary"]


def test_run_partial_status_flows_through(monkeypatch, db):
    # status=partial must survive so the executor's handoff gate fails the step.
    _add_provider(db)
    env = {"answer": "half done", "downstream_handoff": {"summary": "x", "status": "partial"}}
    _stub_chat(monkeypatch, json.dumps(env))
    out = asyncio.run(ClaudeCodeOperationRunner(db).run(question="q", operation_config={}))
    assert out["downstream_handoff"]["status"] == "partial"


def test_run_without_provider_raises(db):
    runner = ClaudeCodeOperationRunner(db)
    with pytest.raises(RuntimeError, match="no enabled Claude Code provider"):
        asyncio.run(runner.run(question="q", operation_config={}))


def test_last_json_object_scans_from_end():
    fn = ClaudeCodeOperationRunner._last_json_object
    assert fn('prefix {"a": 1} middle {"b": 2}')["b"] == 2
    assert fn('{"a": {"nested": true}}')["a"]["nested"] is True
    assert fn("no json here") is None
