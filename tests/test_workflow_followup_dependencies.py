"""Follow-up edges (on_success_follow_up / on_failure_follow_up) act as implicit
CONDITIONAL dependencies: a follow-up target waits for its source and only runs on the
matching outcome; the non-taken branch is skipped. Regression for run 197 where
follow-up ops ran in parallel with the still-running source.
"""
from __future__ import annotations

from types import SimpleNamespace

from services.workflows.workflow_executor import WorkflowExecutor


def _op(op_id, *, depends_on=None, on_success=None, on_failure=None):
    return SimpleNamespace(
        id=op_id,
        depends_on=depends_on or [],
        on_success_follow_up=on_success,
        on_failure_follow_up=on_failure,
    )


def _ex():
    return WorkflowExecutor(workflow_repository=None, run_repository=None, assistant_runner=None)


def _ctx(ops, statuses):
    return {"operations": ops, "operation_statuses": statuses, "operation_outputs": {}, "workflow_run_id": 1}


def test_followup_targets_wait_for_running_source():
    # The run-197 topology: A -> B (on success), A -> C (on failure), B/C have no depends_on.
    a, b, c = _op(1, on_success=2, on_failure=3), _op(2), _op(3)
    ctx = _ctx([a, b, c], {})  # A not finished yet
    ex = _ex()
    assert ex._dependencies_satisfied(a, ctx, set()) is True       # root is ready
    assert ex._dependencies_satisfied(b, ctx, set()) is False      # success target waits
    assert ex._dependencies_satisfied(c, ctx, set()) is False      # failure target waits


def test_on_success_runs_success_branch_skips_failure_branch():
    a, b, c = _op(1, on_success=2, on_failure=3), _op(2), _op(3)
    ctx = _ctx([a, b, c], {1: "success"})
    ex = _ex()
    assert ex._dependencies_satisfied(b, ctx, set()) is True       # success target now ready
    assert ex._dependencies_satisfied(c, ctx, set()) is False      # failure target not taken
    assert ex._followup_branch_dead(c, ctx) is True                # ... and dead -> skipped
    assert ex._followup_branch_dead(b, ctx) is False


def test_on_failure_runs_failure_branch_skips_success_branch():
    a, b, c = _op(1, on_success=2, on_failure=3), _op(2), _op(3, depends_on=[1])
    ctx = _ctx([a, b, c], {1: "failed"})
    ex = _ex()
    assert ex._followup_branch_dead(b, ctx) is True                # success branch dead on failure
    # failure target ready once allowed-after-failure is recorded (as the loop does)
    assert ex._dependencies_satisfied(c, ctx, {(1, 3)}) is True


def test_plain_depends_on_unchanged():
    # No follow-up edges: behaves exactly as before.
    x = _op(1)
    y = _op(2, depends_on=[1])
    ctx = _ctx([x, y], {})
    ex = _ex()
    assert ex._dependencies_satisfied(y, ctx, set()) is False      # waits for X
    assert ex._dependencies_satisfied(y, _ctx([x, y], {1: "success"}), set()) is True
    assert ex._followup_branch_dead(y, ctx) is False               # not a follow-up target
