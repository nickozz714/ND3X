"""The run-completion email carries the actual result (via _result_summary)."""
from __future__ import annotations

from services.workflows.workflow_executor import WorkflowExecutor


def _ex():
    return WorkflowExecutor(workflow_repository=None, run_repository=None, assistant_runner=None)


def test_summary_prefers_top_level_answer():
    assert "Done: 42" in (_ex()._result_summary({"answer": "Done: 42"}) or "")


def test_summary_from_last_operation_output():
    res = {"operation_outputs": {1: {"answer": "first step"}, 2: {"answer": "the final answer"}}}
    out = _ex()._result_summary(res) or ""
    assert "final answer" in out


def test_summary_falls_back_to_handoff_summary():
    res = {"operation_outputs": {3: {"downstream_handoff": {"summary": "handoff result"}}}}
    assert "handoff result" in (_ex()._result_summary(res) or "")


def test_summary_none_when_empty():
    assert _ex()._result_summary({}) is None
    assert _ex()._result_summary({"operation_outputs": {}}) is None
