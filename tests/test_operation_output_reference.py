"""Foolproof 'take the output of step X' reference: ${operation.X.output} resolves to the
operation's primary value (agent markdown answer, tool content, …) without knowing shape."""
from __future__ import annotations

from types import SimpleNamespace

from services.workflows.workflow_executor import WorkflowExecutor


def _ex() -> WorkflowExecutor:
    return WorkflowExecutor.__new__(WorkflowExecutor)


def test_output_value_per_type():
    ex = _ex()
    assert ex._operation_output_value({"mode": "final", "answer": "# Report\nbody"}) == "# Report\nbody"
    assert ex._operation_output_value({"mode": "tool", "result": {"content": "hello"}}) == "hello"
    assert ex._operation_output_value({"mode": "tool", "result": {"id": 7}}) == {"id": 7}
    assert ex._operation_output_value({"downstream_handoff": {"summary": "s"}}) == "s"


def test_reference_output_by_id_name_and_alias():
    ex = _ex()
    ctx = {
        "operation_outputs": {3: {"mode": "final", "answer": "# md report"}},
        "operations": [SimpleNamespace(id=3, name="Report")],
    }
    for expr in ("operation.3.output", "operation.Report.output", "operation.3.output.value",
                 "operation_output.3.output"):
        ok, val = ex._resolve_reference(expr, ctx)
        assert ok and val == "# md report", expr


def test_previous_operation_output_canonical():
    ex = _ex()
    ctx = {"operation_outputs": {2: {"mode": "tool", "result": {"content_text": "doc body"}}}, "operations": []}
    ok, val = ex._resolve_reference("previous_operation_output.output", ctx, previous_operation_id=2)
    assert ok and val == "doc body"


def test_raw_paths_still_work():
    ex = _ex()
    ctx = {"operation_outputs": {1: {"answer": "x", "downstream_handoff": {"facts": {"k": 9}}}},
           "operations": []}
    ok, val = ex._resolve_reference("operation.1.downstream_handoff.facts.k", ctx)
    assert ok and val == 9
