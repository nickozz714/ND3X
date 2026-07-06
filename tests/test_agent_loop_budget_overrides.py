"""Per-operation agent-loop budget overrides (manual control of the wall-clock/iteration
limits). 0 = no time limit, but bounded by the hard ceiling so it can't run forever."""
from __future__ import annotations

from services.assistants.orchestration.pipeline_runner import _agent_loop_budgets


def test_defaults_present():
    b = _agent_loop_budgets(True)
    assert b["max_wall_clock_seconds"] == 600
    assert b["max_iterations"] >= 1


def test_override_raises_wall_clock():
    # An override raises the default 600 — up to the hard ceiling (default 1800).
    b = _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 1200})
    assert b["max_wall_clock_seconds"] == 1200


def test_zero_wall_clock_bounded_by_hard_ceiling():
    # 0 ("no limit") is now bounded by WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS
    # so a wandering agent can never run forever. See test_activity_tool_scope_and_budget
    # for the ceiling behaviour incl. disabling it (hard=0 → truly unbounded).
    b = _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 0})
    assert b["max_wall_clock_seconds"] == 1800


def test_iteration_override_must_stay_positive():
    # iterations/tool_calls keep a hard floor of 1 (no "unlimited") so the loop always stops
    b = _agent_loop_budgets(True, overrides={"max_iterations": 0, "max_tool_calls": 50})
    assert b["max_iterations"] == 12  # invalid 0 ignored → default kept
    assert b["max_tool_calls"] == 50


def test_garbage_overrides_ignored():
    b = _agent_loop_budgets(False, overrides={"max_wall_clock_seconds": "lots", "max_iterations": -3})
    assert b["max_wall_clock_seconds"] == 300
    assert b["max_iterations"] == 12  # chat default
