"""chat.memory_decision slot semantics (user decision 2026-07-04): assigned →
that model makes the memory-retrieval decision; UNASSIGNED → the decision step
is OFF — it must not silently borrow the planner model."""
from __future__ import annotations

import asyncio

from services.system_cognition.system_cognition_service import SystemCognitionService


def test_memory_decision_slot_is_canonical():
    from services.providers.capability_router import ALL_SLOTS
    assert "chat.memory_decision" in ALL_SLOTS


def test_disabled_result_shape():
    out = SystemCognitionService._memory_decision_disabled_result()
    assert out["ok"] is True
    assert out["should_retrieve"] is False
    assert "unassigned" in out["reason"]


def test_planner_decide_skips_model_call_when_slot_unassigned(monkeypatch):
    svc = SystemCognitionService.__new__(SystemCognitionService)  # no full init needed

    monkeypatch.setattr(SystemCognitionService, "_memory_decision_off", staticmethod(lambda: True))

    called = {"run": False}

    class _Runner:
        async def run(self, **kwargs):
            called["run"] = True
            return {"ok": True}

    svc.system_runner = _Runner()
    svc.planner_memory_retrieval_decision_assistant = object()

    out = asyncio.run(svc.decide_planner_memory_retrieval(
        question="q", active_conversation_state=None, thread_id="t",
        project_id=None, turn_id=1, trace=[],
    ))
    assert out["should_retrieve"] is False
    assert called["run"] is False  # no model call happened


def test_router_decide_runs_when_slot_assigned(monkeypatch):
    svc = SystemCognitionService.__new__(SystemCognitionService)

    monkeypatch.setattr(SystemCognitionService, "_memory_decision_off", staticmethod(lambda: False))

    class _Runner:
        async def run(self, **kwargs):
            return {"ok": True, "data": {"should_retrieve": True, "scopes": [], "types": []}}

    svc.system_runner = _Runner()
    svc.router_memory_retrieval_decision_assistant = object()

    out = asyncio.run(svc.decide_router_memory_retrieval(
        question="q", active_conversation_state=None, thread_id="t",
        project_id=None, turn_id=1, trace=[],
    ))
    # the real runner path was taken (result comes from _Runner, not the disabled stub)
    assert "unassigned" not in (out.get("reason") or "")


def test_explicit_model_bypasses_slot_gate(monkeypatch):
    """An explicitly passed model (e.g. from tests/tools) still runs the step."""
    svc = SystemCognitionService.__new__(SystemCognitionService)
    monkeypatch.setattr(SystemCognitionService, "_memory_decision_off", staticmethod(lambda: True))

    called = {"run": False}

    class _Runner:
        async def run(self, **kwargs):
            called["run"] = True
            return {"ok": True, "data": {"should_retrieve": False, "scopes": [], "types": []}}

    svc.system_runner = _Runner()
    svc.planner_memory_retrieval_decision_assistant = object()

    asyncio.run(svc.decide_planner_memory_retrieval(
        question="q", active_conversation_state=None, thread_id="t",
        project_id=None, turn_id=1, trace=[], model="some-model",
    ))
    assert called["run"] is True
