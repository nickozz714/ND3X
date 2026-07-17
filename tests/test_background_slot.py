"""Background agents: the chat.background routing slot + the no-fallback dispatch
gate. A dispatched/background subagent resolves its model from chat.background
(or a per-call override); an unassigned slot refuses the dispatch — no silent
fallback to the foreground planner.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from schemas.provider import ProviderCreate, ProviderModelCreate
from services.providers.capability_router import ALL_SLOTS
from services.providers.execution_mode import CAP_CLASS, OUTSOURCEABLE
from services.providers.registry_service import ProviderRegistryService


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


# ── slot registration ─────────────────────────────────────────────────────────
def test_slot_registered_and_outsourceable():
    assert "chat.background" in ALL_SLOTS
    assert CAP_CLASS["chat.background"] == OUTSOURCEABLE  # a CLI agent may drive it
    assert "chat.background" in pv.ROUTING_SLOTS


# ── resolve_background_model ───────────────────────────────────────────────────
def test_per_call_model_wins_without_db():
    from services.builtin.tools.agent_tools import resolve_background_model
    # explicit override short-circuits — no registry lookup needed
    assert resolve_background_model("gpt-5-mini") == ("gpt-5-mini", None)


def test_resolves_assigned_slot(db, monkeypatch):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="qwen2.5:14b", capability="chat"))
    reg.set_assignment("chat.background", m.id)

    import db.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: db)
    from services.builtin.tools.agent_tools import resolve_background_model
    model, err = resolve_background_model(None)
    assert err is None and model == "qwen2.5:14b"


def test_unassigned_slot_refuses(db, monkeypatch):
    import db.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: db)  # empty registry — no assignment
    from services.builtin.tools.agent_tools import resolve_background_model
    model, err = resolve_background_model(None)
    assert model is None and err and "chat.background" in err


# ── agent_dispatch honors the gate ─────────────────────────────────────────────
def _fake_orchestrator(monkeypatch, sink):
    import sys
    import types
    mod = types.ModuleType("services.assistants.ask_job_callbacks")

    async def fake(*, question, payload, thread_id, model):
        sink.append({"payload": dict(payload), "model": model})
        return {"mode": "final", "answer": "ok", "terminal_state": "completed",
                "downstream_handoff": {"summary": "done"}}

    mod.run_ask_orchestrator = fake
    monkeypatch.setitem(sys.modules, "services.assistants.ask_job_callbacks", mod)


def test_dispatch_forces_background_model(monkeypatch):
    from services.builtin.tools import agent_tools
    monkeypatch.setattr(agent_tools, "resolve_background_model", lambda m: (m or "bg-model", None))
    sink = []
    _fake_orchestrator(monkeypatch, sink)

    res = asyncio.run(agent_tools.agent_dispatch({"task": "do it"}))
    assert res["status"] == "ok"
    # the whole subagent run is forced onto the resolved background model
    assert sink[-1]["model"] == "bg-model"
    assert sink[-1]["payload"]["forced_model"] == "bg-model"


def test_dispatch_refused_when_slot_unset(monkeypatch):
    from services.builtin.tools import agent_tools
    monkeypatch.setattr(agent_tools, "resolve_background_model",
                        lambda m: (None, "Background agents have no model. …"))
    sink = []
    _fake_orchestrator(monkeypatch, sink)

    res = asyncio.run(agent_tools.agent_dispatch({"task": "do it"}))
    assert res["status"] == "error" and "no model" in res["error"]
    assert sink == []  # never reached the orchestrator


def test_dispatch_per_call_model_override(monkeypatch):
    from services.builtin.tools import agent_tools
    monkeypatch.setattr(agent_tools, "resolve_background_model", lambda m: (m or "bg-model", None))
    sink = []
    _fake_orchestrator(monkeypatch, sink)

    res = asyncio.run(agent_tools.agent_dispatch({"task": "x", "model": "claude-opus-4-8"}))
    assert res["status"] == "ok"
    assert sink[-1]["model"] == "claude-opus-4-8"  # override wins over the slot


# ── task_create gates BEFORE spawning ──────────────────────────────────────────
def test_task_create_refused_when_slot_unset(monkeypatch):
    from services.builtin.tools import agent_tools, background_tasks
    monkeypatch.setattr(agent_tools, "resolve_background_model",
                        lambda m: (None, "Background agents have no model."))
    monkeypatch.setattr(background_tasks, "_TASKS", {})  # isolate the shared registry
    res = asyncio.run(background_tasks.task_create({"task": "bg work"}))
    assert res["status"] == "error" and "no model" in res["error"].lower()
    # nothing was registered / spawned
    assert background_tasks._TASKS == {}


def test_task_create_injects_resolved_model(monkeypatch):
    """When the slot resolves, task_create injects the model into args and spawns.
    Run the detached subagent inline (patched create_task) to capture the args."""
    from services.builtin.tools import agent_tools, background_tasks
    monkeypatch.setattr(agent_tools, "resolve_background_model", lambda m: (m or "bg-model", None))
    monkeypatch.setattr(background_tasks, "_TASKS", {})

    captured = {}

    async def fake_dispatch(args):
        captured.update(args)
        return {"status": "ok", "summary": "done"}

    monkeypatch.setattr(agent_tools, "agent_dispatch", fake_dispatch)

    async def scenario():
        real_create_task = asyncio.get_running_loop().create_task
        spawned = []
        monkeypatch.setattr(background_tasks.asyncio, "create_task",
                            lambda coro, **kw: spawned.append(real_create_task(coro)) or spawned[-1])
        res = await background_tasks.task_create({"task": "bg work"})
        await asyncio.gather(*spawned)  # drain the detached task
        return res

    res = asyncio.run(scenario())
    assert res["status"] == "started" and res["task_id"].startswith("bg-")
    assert captured.get("model") == "bg-model"  # resolved model injected into the run
