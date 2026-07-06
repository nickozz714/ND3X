"""
services/workflows/workflow_run_recovery.py

Crash/restart recovery for in-flight workflow runs.

Runs execute in-process (WorkflowWorker + BackgroundTasks). When the process
dies mid-run (a deploy/restart, or a --reload during development) the executor is
gone but the DB row stays `running` forever — the run hangs and blocks nothing,
but shows as active indefinitely (observed: a run stuck `running` for 2 months).

At startup we fail such orphaned runs. A run is orphaned only if its LAST
ACTIVITY (run.started_at, or any operation-run started_at/heartbeat) is older
than WORKFLOW_ORPHAN_THRESHOLD_MINUTES — comfortably beyond the per-operation
wall-clock budget — so a legitimately long, still-live run (or one a sibling
worker is actively heartbeating) is never reaped. Only `running` /
`cancel_requested` runs are touched: `queued` runs are picked up by the worker
and `waiting` runs are resumable.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session, selectinload

from component.config import settings
from component.logging import get_logger

log = get_logger(__name__)

_INTERRUPT_ERROR = (
    "Interrupted: the workflow executor stopped (likely a process restart) while "
    "this run was in progress. Workflow runs are not resumable across restarts — "
    "trigger the workflow again."
)


def _last_activity(run) -> Optional[datetime]:
    latest = run.started_at or run.created_at
    for op in (getattr(run, "operation_runs", None) or []):
        for ts in (op.last_heartbeat_at, op.started_at, op.finished_at):
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    return latest


def recover_orphaned_runs(
    db: Session,
    *,
    threshold_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from models.workflow import WorkflowRun
    from repository.workflow_run_repository import TERMINAL_OPERATION_RUN_STATUSES

    now = now or datetime.utcnow()
    threshold = (
        threshold_minutes
        if threshold_minutes is not None
        else int(getattr(settings, "WORKFLOW_ORPHAN_THRESHOLD_MINUTES", 30))
    )
    cutoff = now - timedelta(minutes=threshold)

    runs = (
        db.query(WorkflowRun)
        .options(selectinload(WorkflowRun.operation_runs))
        .filter(WorkflowRun.status.in_(["running", "cancel_requested"]))
        .all()
    )

    recovered: list[dict[str, Any]] = []
    skipped_fresh = 0
    for run in runs:
        activity = _last_activity(run)
        if activity is not None and activity > cutoff:
            skipped_fresh += 1
            continue  # still fresh — a live worker may own it
        new_status = "cancelled" if run.status == "cancel_requested" else "failed"
        for op in (run.operation_runs or []):
            if op.status not in TERMINAL_OPERATION_RUN_STATUSES:
                op.status = new_status
                op.error = _INTERRUPT_ERROR
                op.finished_at = now
        run.status = new_status
        run.error = _INTERRUPT_ERROR
        run.finished_at = now
        recovered.append({
            "run_id": run.id,
            "workflow_id": run.workflow_id,
            "new_status": new_status,
            "last_activity": activity.isoformat() if activity else None,
        })

    if recovered:
        db.commit()
        log.warningx(
            "workflow_run_recovery:orphans_failed",
            count=len(recovered),
            threshold_minutes=threshold,
            run_ids=[r["run_id"] for r in recovered],
        )
    elif skipped_fresh:
        log.infox("workflow_run_recovery:all_fresh", active=skipped_fresh)
    return {"recovered": len(recovered), "skipped_fresh": skipped_fresh, "runs": recovered}
