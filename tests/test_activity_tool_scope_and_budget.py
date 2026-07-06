"""Per-activity builtin-tool allowlist + the hard wall-clock ceiling that bounds
a 'no limit' (0) workflow operation."""
from __future__ import annotations

import pytest

from component.config import settings
from services.assistants.orchestration.pipeline_runner import _agent_loop_budgets
from services.assistants.orchestration.runtime import RuntimeResolver


# ── hard wall-clock ceiling ───────────────────────────────────────────────
@pytest.fixture
def hard_cap(monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS", 1800, raising=False)
    monkeypatch.setattr(settings, "WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS", 600, raising=False)


def test_no_limit_is_bounded_by_hard_cap(hard_cap):
    b = _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 0})
    assert b["max_wall_clock_seconds"] == 1800


def test_override_above_cap_is_clamped(hard_cap):
    b = _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 99999})
    assert b["max_wall_clock_seconds"] == 1800


def test_override_below_cap_is_respected(hard_cap):
    b = _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 300})
    assert b["max_wall_clock_seconds"] == 300


def test_default_workflow_budget_unchanged(hard_cap):
    assert _agent_loop_budgets(True)["max_wall_clock_seconds"] == 600


def test_chat_is_not_subject_to_workflow_cap(hard_cap, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_AGENT_MAX_WALL_CLOCK_SECONDS", 300, raising=False)
    # A chat override of 0 (no limit) is NOT clamped by the workflow ceiling.
    assert _agent_loop_budgets(False, overrides={"max_wall_clock_seconds": 0})["max_wall_clock_seconds"] == 0


def test_hard_cap_zero_disables_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS", 0, raising=False)
    assert _agent_loop_budgets(True, overrides={"max_wall_clock_seconds": 0})["max_wall_clock_seconds"] == 0


# ── per-activity builtin-tool allowlist ───────────────────────────────────
class _Tool:
    def __init__(self, name):
        self.name = name


class _Config:
    def __init__(self):
        self.tools = [_Tool("text__search"), _Tool("fabric_data_agent_query"), _Tool("csv_profile")]


def _resolver():
    r = RuntimeResolver.__new__(RuntimeResolver)
    r.runtime_loader = type("L", (), {"get_single_agent": staticmethod(lambda: _Config())})()
    r.runtime_factory = type("F", (), {"create": staticmethod(lambda cfg: cfg)})()
    return r


def test_allowlist_filters_builtin_tools():
    a = _resolver().get_single_agent_runtime_assistant(allowed_builtin_tools=["text__search", "csv_profile"])
    assert {t.name for t in a.tools} == {"text__search", "csv_profile"}
    assert "fabric_data_agent_query" not in {t.name for t in a.tools}


def test_no_allowlist_keeps_all_builtins():
    a = _resolver().get_single_agent_runtime_assistant()
    assert {t.name for t in a.tools} == {"text__search", "fabric_data_agent_query", "csv_profile"}
