"""Workflows remember the whole run: each operation's agent gets the prior steps +
outcomes as conversation state (provider-equal client-side transcript)."""
from __future__ import annotations

from types import SimpleNamespace

from services.workflows.workflow_executor import WorkflowExecutor


def _executor() -> WorkflowExecutor:
    # Helpers under test only use self._compact_preview (pure), so skip __init__.
    return WorkflowExecutor.__new__(WorkflowExecutor)


def test_summarize_prefers_handoff_then_answer_then_error():
    ex = _executor()
    assert ex._summarize_operation_output({"downstream_handoff": {"summary": "did X"}}) == "did X"
    assert ex._summarize_operation_output({"answer": "the answer"}) == "the answer"
    assert ex._summarize_operation_output({"error": "boom"}).startswith("FAILED: boom")
    assert "v1" in ex._summarize_operation_output({"mode": "set_variable", "variables_set": {"v1": 1}})


def test_run_transcript_includes_completed_ops_in_order():
    ex = _executor()
    ops = [
        SimpleNamespace(id=1, name="Fetch data", operation_type="assistant"),
        SimpleNamespace(id=2, name="Summarize", operation_type="assistant"),
        SimpleNamespace(id=3, name="Not run yet", operation_type="assistant"),
    ]
    context = {
        "operations": ops,
        "operation_outputs": {
            1: {"answer": "fetched 10 rows"},
            2: {"downstream_handoff": {"summary": "summary ready"}},
        },
        "operation_statuses": {1: "success", 2: "success"},
    }
    msgs = ex._workflow_run_transcript(context)

    assert len(msgs) == 2                                  # only completed ops, current one excluded
    assert all(m["role"] == "assistant" for m in msgs)
    assert "Fetch data" in msgs[0]["content"] and "fetched 10 rows" in msgs[0]["content"]
    assert "Summarize" in msgs[1]["content"] and "summary ready" in msgs[1]["content"]
    assert "Not run yet" not in msgs[1]["content"]


def test_run_transcript_empty_when_no_completed_ops():
    ex = _executor()
    assert ex._workflow_run_transcript({"operations": [], "operation_outputs": {}}) == []
