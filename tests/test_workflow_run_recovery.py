"""Orphaned-run recovery: stale `running` runs are failed at startup; fresh,
queued and waiting runs are left alone."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.workflow import Workflow, WorkflowRun, WorkflowOperationRun
from services.workflows.workflow_run_recovery import recover_orphaned_runs

_NOW = datetime(2026, 7, 5, 22, 0, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Workflow.__table__, WorkflowRun.__table__, WorkflowOperationRun.__table__],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _wf(db) -> Workflow:
    wf = Workflow(name="wf", input_schema={}, is_enabled=True)
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return wf


def _run(db, wf_id, status, started_at, *, op_status=None, op_heartbeat=None) -> WorkflowRun:
    run = WorkflowRun(workflow_id=wf_id, trigger_type="manual", status=status,
                      input_payload={}, started_at=started_at, created_at=started_at)
    db.add(run)
    db.commit()
    db.refresh(run)
    if op_status is not None:
        op = WorkflowOperationRun(workflow_run_id=run.id, workflow_operation_id=1,
                                  status=op_status, input_payload={},
                                  started_at=started_at, last_heartbeat_at=op_heartbeat)
        db.add(op)
        db.commit()
    return run


def test_stale_running_run_is_failed(db):
    wf = _wf(db)
    run = _run(db, wf.id, "running", _NOW - timedelta(hours=3), op_status="running")
    out = recover_orphaned_runs(db, now=_NOW)
    assert out["recovered"] == 1
    db.refresh(run)
    assert run.status == "failed" and run.finished_at is not None and "Interrupted" in run.error
    op = db.query(WorkflowOperationRun).filter_by(workflow_run_id=run.id).first()
    assert op.status == "failed"


def test_fresh_running_run_is_left_alone(db):
    wf = _wf(db)
    # Heartbeat 2 minutes ago → within the 30-min threshold.
    run = _run(db, wf.id, "running", _NOW - timedelta(hours=1),
               op_status="running", op_heartbeat=_NOW - timedelta(minutes=2))
    out = recover_orphaned_runs(db, now=_NOW)
    assert out["recovered"] == 0 and out["skipped_fresh"] == 1
    db.refresh(run)
    assert run.status == "running"


def test_queued_and_waiting_runs_are_untouched(db):
    wf = _wf(db)
    queued = _run(db, wf.id, "queued", _NOW - timedelta(hours=5))
    waiting = _run(db, wf.id, "waiting", _NOW - timedelta(hours=5))
    recover_orphaned_runs(db, now=_NOW)
    db.refresh(queued); db.refresh(waiting)
    assert queued.status == "queued" and waiting.status == "waiting"


def test_cancel_requested_stale_becomes_cancelled(db):
    wf = _wf(db)
    run = _run(db, wf.id, "cancel_requested", _NOW - timedelta(hours=2), op_status="running")
    recover_orphaned_runs(db, now=_NOW)
    db.refresh(run)
    assert run.status == "cancelled"
    op = db.query(WorkflowOperationRun).filter_by(workflow_run_id=run.id).first()
    assert op.status == "cancelled"


def test_terminal_operation_runs_are_preserved(db):
    wf = _wf(db)
    run = _run(db, wf.id, "running", _NOW - timedelta(hours=3), op_status="success")
    recover_orphaned_runs(db, now=_NOW)
    op = db.query(WorkflowOperationRun).filter_by(workflow_run_id=run.id).first()
    assert op.status == "success"  # already-finished operation not clobbered
