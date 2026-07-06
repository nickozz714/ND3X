"""A workflow operation's pinned model must win over the role's routing slot —
it is applied via forced_chat_model (like the chat model picker), scoped to the
operation and reset afterwards. Regression: workflows silently ran on the local
chat.planner slot model (~280s/hop) instead of the pinned cloud model (~3s)."""
from __future__ import annotations

import asyncio

import pytest

from component.config import settings
from services.providers.chat_session import forced_chat_model
from services.workflows.assistant_operation_runner import AssistantOperationRunner


class _FakeAssistant:
    name = "Agent"
    tools: list = []


class _FakeRuntime:
    def get_single_agent_runtime_assistant(self, **_kw):
        return _FakeAssistant()


def _make_runner(capture):
    class _FakePipeline:
        async def run(self, **kw):
            capture["forced_during_run"] = forced_chat_model.get()
            capture["payload"] = kw.get("payload") or {}
            return {"mode": "final", "answer": "ok"}

    runner = AssistantOperationRunner.__new__(AssistantOperationRunner)
    runner.runtime = _FakeRuntime()
    runner.pipeline_runner = _FakePipeline()
    return runner


@pytest.fixture(autouse=True)
def single_agent(monkeypatch):
    monkeypatch.setattr(settings, "SINGLE_AGENT_MODE", True, raising=False)


def _run(runner, model):
    return asyncio.run(runner.run(
        assistant_id=1,
        question="q",
        payload={"operation_config": {"model": model}},
        workflow_run_id=1,
        operation_id=1,
        model=model,
    ))


def test_pinned_model_forced_during_run_and_reset_after():
    capture = {}
    runner = _make_runner(capture)
    assert forced_chat_model.get() is None
    _run(runner, "gpt-5.4-mini")
    assert capture["forced_during_run"] == "gpt-5.4-mini"  # override wins over the slot
    assert forced_chat_model.get() is None  # scoped: reset after the operation


def test_no_pinned_model_leaves_forced_unset():
    capture = {}
    runner = _make_runner(capture)
    asyncio.run(runner.run(
        assistant_id=1, question="q", payload={"operation_config": {}},
        workflow_run_id=1, operation_id=1, model=None,
    ))
    assert capture["forced_during_run"] is None


def _run_with_cfg(cfg):
    capture = {}
    runner = _make_runner(capture)
    asyncio.run(runner.run(
        assistant_id=1, question="q", payload={"operation_config": cfg},
        workflow_run_id=1, operation_id=1, model=None,
    ))
    return capture["payload"]


def test_light_mode_on_forces_session_flag_true():
    assert _run_with_cfg({"light_mode": "on"}).get("_light_mode_session") is True


def test_light_mode_off_forces_session_flag_false():
    assert _run_with_cfg({"light_mode": "off"}).get("_light_mode_session") is False


def test_light_mode_auto_leaves_session_flag_unset():
    # auto → per-model behaviour, no forced override
    assert "_light_mode_session" not in _run_with_cfg({"light_mode": "auto"})
    assert "_light_mode_session" not in _run_with_cfg({})
