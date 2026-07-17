"""Tests for the agent capability features (parallel tool calls, subagent
dispatch, background tasks, verification hop) and the code-authoritative
system-assistant schemas/instructions.

Async paths are driven via asyncio.run() so no pytest-asyncio config is needed.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from services.assistants.runtime import system_assistants as sa
from services.assistants.runtime_config import AssistantConfig
from services.assistants.orchestration.tool_execution import (
    ToolExecutionRunner,
    _call_is_parallel_eligible,
    _args_reference_prior_results,
)
from services.assistants.orchestration.pipeline_runner import _verification_findings


# ──────────────────────────────────────────────────────────────────────────────
# Code-authoritative schemas / instructions
# ──────────────────────────────────────────────────────────────────────────────

def test_schema_override_by_type():
    assert sa.schema_for_type("planner")["properties"]["action"]["enum"] == [
        "tool_calls", "final", "ask_user", "propose_plan", "select_skills"
    ]
    assert sa.schema_for_type("router")["properties"]["mode"]["enum"][0] == "single"
    assert sa.schema_for_type("final_answer") == {}
    assert sa.schema_for_type("unknown") is None


def test_planner_instruction_stays_editable_schema_forced():
    cfg = AssistantConfig(assistant_type="planner", instruction="USER EDITED", schema={"old": True})
    sa.apply_system_overrides(cfg)
    assert cfg.instruction == "USER EDITED"          # planner instruction preserved
    assert cfg.schema == sa.schema_for_type("planner")  # schema forced from code


def test_router_schema_and_instruction_forced():
    cfg = AssistantConfig(assistant_type="router", instruction="STALE", schema={"old": True})
    sa.apply_system_overrides(cfg)
    assert cfg.instruction == sa.instruction_override_for_type("router")
    assert cfg.schema == sa.schema_for_type("router")
    assert sa.instruction_override_for_type("planner") is None


def test_schema_is_mutation_isolated():
    s = sa.schema_for_type("planner")
    s["__mutated__"] = 1
    assert "__mutated__" not in sa.schema_for_type("planner")


def test_capabilities_primer():
    primer = sa.capabilities_primer_for_type("planner")
    assert "agent__dispatch" in primer and "task__create" in primer
    assert sa.capabilities_primer_for_type("router") != ""
    assert sa.capabilities_primer_for_type("final_answer") == ""


# ──────────────────────────────────────────────────────────────────────────────
# Parallel tool execution
# ──────────────────────────────────────────────────────────────────────────────

def test_parallel_eligibility():
    assert _call_is_parallel_eligible("search", {"q": "x"}) is True
    assert _call_is_parallel_eligible("search", {"q": "${result.0.id}"}) is False  # dependency
    assert _call_is_parallel_eligible("system__shell_exec", {"cmd": "ls"}) is False  # guarded
    assert _args_reference_prior_results({"a": ["${last.items}"]}) is True
    assert _args_reference_prior_results({"a": "plain"}) is False


def test_parallel_scheduling_orders_concurrency_and_serializes_guarded():
    concurrency = {"now": 0, "max": 0}
    order = []

    class Runner(ToolExecutionRunner):
        async def _run_single_tool_call(self, *, tc, results, **kw):
            idx = tc["args"]["_idx"]
            order.append(("start", idx))
            concurrency["now"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["now"])
            await asyncio.sleep(0.03)
            concurrency["now"] -= 1
            order.append(("end", idx))
            return {"status": "ok", "idx": idx}

    def mk(idx, tool="search", dep=False):
        args = {"_idx": idx}
        if dep:
            args["q"] = "${result.0.idx}"
        return {"tool": tool, "tool_id": 1, "args": args}

    runner = Runner(tool_execution_service=None, ingest_wait_timeout_s=1,
                    ingest_poll_interval_s=1, max_tool_calls_per_turn=50)
    # 2 independent, 1 guarded, 1 independent, 1 dependent
    calls = [mk(0), mk(1), mk(2, tool="system__shell_exec"), mk(3), mk(4, dep=True)]

    results = asyncio.run(runner.execute_tool_calls(
        tool_calls=calls, session_id="s", turn_id=1, trace=[],
        assistant_name="A", trace_fn=lambda *a, **k: None, preview_fn=lambda x: x,
        progress_cb=None, confirmed_tool_call_hashes=None))

    assert [r["idx"] for r in results] == [0, 1, 2, 3, 4]    # index order preserved
    assert concurrency["max"] >= 2                            # 0 and 1 ran together
    first_start = lambda i: order.index(("start", i))
    end = lambda i: order.index(("end", i))
    assert first_start(2) > end(0) and first_start(2) > end(1)  # guarded after batch
    assert first_start(4) > end(3)                              # dependent serialized


# ──────────────────────────────────────────────────────────────────────────────
# Verification self-check
# ──────────────────────────────────────────────────────────────────────────────

def test_verification_findings():
    assert _verification_findings("A complete answer.", {"_acc_tool_results": [{"status": "ok"}]}) == []
    assert any("empty" in x for x in _verification_findings("  ", {}))
    # recoverable error is NOT flagged (avoid false positives)
    assert _verification_findings("ok", {"_acc_tool_results": [{"status": "error", "recoverable": True}]}) == []
    # unrecoverable error IS flagged
    bad = _verification_findings("ok", {"_acc_tool_results": [{"status": "error", "recoverable": False, "tool": "shell"}]})
    assert any("unrecoverably" in x for x in bad)


# ──────────────────────────────────────────────────────────────────────────────
# Subagent dispatch
# ──────────────────────────────────────────────────────────────────────────────

def _install_fake_orchestrator(monkeypatch, fake):
    """Inject a fake ask_job_callbacks module so agent_dispatch's lazy import
    resolves to our fake run_ask_orchestrator without importing the heavy real one."""
    mod = types.ModuleType("services.assistants.ask_job_callbacks")
    mod.run_ask_orchestrator = fake
    monkeypatch.setitem(sys.modules, "services.assistants.ask_job_callbacks", mod)


def test_agent_dispatch_named_and_condenses(monkeypatch):
    from services.builtin.tools import agent_tools

    # A background model must resolve (chat.background slot / override); stub it.
    monkeypatch.setattr(agent_tools, "resolve_background_model", lambda m: (m or "bg-model", None))

    calls = []

    async def fake(*, question, payload, thread_id, model):
        calls.append(dict(payload))
        return {"mode": "final", "answer": "x" * 50, "terminal_state": "completed",
                "tool_calls": [1],
                "downstream_handoff": {"summary": "did it", "facts": {"k": 1}, "artifacts": [], "open_questions": []}}

    _install_fake_orchestrator(monkeypatch, fake)

    res = asyncio.run(agent_tools.agent_dispatch({"task": "research X", "assistant": "Researcher"}))
    assert res["status"] == "ok"
    assert res["summary"] == "did it" and res["facts"] == {"k": 1}
    assert calls[-1]["force_assistant"] == "Researcher"
    assert calls[-1]["_subagent_depth"] == 1
    assert calls[-1]["forced_model"] == "bg-model"


def test_agent_dispatch_depth_guard(monkeypatch):
    from services.builtin.tools import agent_tools

    async def fake(*, question, payload, thread_id, model):
        return {"mode": "final", "answer": "y"}

    _install_fake_orchestrator(monkeypatch, fake)
    token = agent_tools._subagent_depth.set(agent_tools.settings.SUBAGENT_MAX_DEPTH)
    try:
        res = asyncio.run(agent_tools.agent_dispatch({"task": "deep"}))
    finally:
        agent_tools._subagent_depth.reset(token)
    assert res["status"] == "error" and "depth" in res["error"]


def test_agent_dispatch_requires_task():
    from services.builtin.tools import agent_tools
    res = asyncio.run(agent_tools.agent_dispatch({"task": "   "}))
    assert res["status"] == "error" and "task" in res["error"]


# ──────────────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────────────

def test_background_task_lifecycle(monkeypatch):
    from services.builtin.tools import background_tasks as bt
    from services.builtin.internal_tool_registry import internal_tool_registry as reg

    bt._TASKS.clear()
    gate = asyncio.Event()

    async def fake_dispatch(args):
        await gate.wait()
        return {"status": "ok", "summary": "bg: " + args.get("task", ""), "thread_id": "sub-x"}

    monkeypatch.setattr("services.builtin.tools.agent_tools.agent_dispatch", fake_dispatch)
    # task_create resolves the background model up front (no-fallback gate) — stub it.
    monkeypatch.setattr("services.builtin.tools.agent_tools.resolve_background_model",
                        lambda m: (m or "bg-model", None))

    create = reg._handlers["task__create"]
    status = reg._handlers["task__status"]
    result = reg._handlers["task__result"]
    listing = reg._handlers["task__list"]

    async def scenario():
        bt.current_run_thread.set("thread-A")
        started = await create({"task": "alpha"})
        assert started["status"] == "started"
        tid = started["task_id"]

        assert (await status({"task_id": tid}))["status"] == "running"
        assert (await result({"task_id": tid}))["status"] == "running"
        assert (await listing({}))["count"] == 1

        gate.set()
        await asyncio.sleep(0.05)

        assert (await status({"task_id": tid}))["status"] == "done"
        res = await result({"task_id": tid})
        assert res["status"] == "done" and "bg: alpha" in res["result"]["summary"]

        drained = await bt.drain_completed_background_tasks("thread-A")
        assert [d["task_id"] for d in drained] == [tid]
        assert await bt.drain_completed_background_tasks("thread-A") == []   # drain-once
        assert await bt.drain_completed_background_tasks("thread-B") == []   # per-thread

    asyncio.run(scenario())
