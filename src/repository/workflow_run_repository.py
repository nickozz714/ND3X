from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session, selectinload

from models.workflow import WorkflowRun, WorkflowOperationRun

TERMINAL_RUN_STATUSES = {"success", "failed", "cancelled"}
WAITING_RUN_STATUSES = {"waiting"}
ACTIVE_RUN_STATUSES = {"queued", "running", "cancel_requested", "waiting"}
WAITING_OPERATION_RUN_STATUSES = {"waiting_for_user_input", "waiting_for_approval"}
TERMINAL_OPERATION_RUN_STATUSES = {"success", "failed", "cancelled"}

class WorkflowRunRepository:
    def __init__(self, db: Session):
        self.db = db

    def _progress_with_history(self, progress_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(progress_payload, dict):
            return None
        history = progress_payload.get("resume_history")
        if not history:
            return None
        return {"resume_history": list(history)}

    def create_run(
        self,
        *,
        workflow_id: int,
        trigger_type: str,
        input_payload: Optional[Dict[str, Any]] = None,
            parent_run_id: int | None = None,
            parent_operation_run_id: int | None = None,
            parent_item_index: int | None = None,
    ) -> WorkflowRun:
        run = WorkflowRun(
            workflow_id=workflow_id,
            trigger_type=trigger_type,
            status="queued",
            input_payload=input_payload or {},
            created_at=datetime.utcnow(),
            parent_run_id=parent_run_id,
            parent_operation_run_id=parent_operation_run_id,
            parent_item_index=parent_item_index,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def update_operation_run_progress(self, operation_run_id: int, progress_payload: dict):
        op_run = self.db.query(WorkflowOperationRun).filter(
            WorkflowOperationRun.id == operation_run_id
        ).first()

        if not op_run:
            return None

        op_run.progress_payload = progress_payload
        op_run.last_heartbeat_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(op_run)
        return op_run

    def append_operation_resume_history(
        self,
        operation_run_id: int,
        item: Dict[str, Any],
    ) -> Optional[WorkflowOperationRun]:
        operation_run = self.get_operation_run(operation_run_id)
        if not operation_run:
            return None
        progress = dict(operation_run.progress_payload or {})
        history = list(progress.get("resume_history") or [])
        history.append(item)
        progress["resume_history"] = history
        operation_run.progress_payload = progress
        operation_run.last_heartbeat_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def get_run(self, run_id: int) -> Optional[WorkflowRun]:
        return (
            self.db.query(WorkflowRun)
            .filter(WorkflowRun.id == run_id)
            .first()
        )

    def get_run_with_operations(self, run_id: int) -> Optional[WorkflowRun]:
        return (
            self.db.query(WorkflowRun)
            .options(selectinload(WorkflowRun.operation_runs))
            .filter(WorkflowRun.id == run_id)
            .first()
        )

    def list_runs_for_workflow(
        self,
        workflow_id: int,
        *,
        skip: int = 0,
        limit: int = 100,
    ):
        return (
            self.db.query(WorkflowRun)
            .filter(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_queued_runs(self, limit: int = 5):
        return (
            self.db.query(WorkflowRun)
            .filter(WorkflowRun.status == "queued")
            .order_by(WorkflowRun.created_at.asc())
            .limit(limit)
            .all()
        )

    def mark_running(self, run_id: int) -> Optional[WorkflowRun]:
        run = self.get_run(run_id)
        if not run:
            return None

        if run.status in TERMINAL_RUN_STATUSES:
            return run

        # Preserve cancel_requested so executor sees it and exits cleanly.
        if run.status == "cancel_requested":
            return run

        run.status = "running"
        if not run.started_at:
            run.started_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)
        return run

    def mark_finished(
            self,
            run_id: int,
            *,
            result_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkflowRun]:
        run = self.get_run(run_id)
        if not run:
            return None

        if run.status in TERMINAL_RUN_STATUSES or run.status == "cancel_requested":
            return run

        run.status = "success"
        run.result_payload = result_payload or {}
        run.finished_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)
        return run

    def mark_failed(
            self,
            run_id: int,
            *,
            error: str,
            result_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkflowRun]:
        run = self.get_run(run_id)
        if not run:
            return None

        if run.status in TERMINAL_RUN_STATUSES:
            return run

        run.status = "failed"
        run.error = error
        run.result_payload = result_payload or {}
        run.finished_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)
        return run


    def mark_waiting(
        self,
        run_id: int,
        *,
        result_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkflowRun]:
        run = self.get_run(run_id)
        if not run:
            return None
        if run.status in TERMINAL_RUN_STATUSES or run.status == "cancel_requested":
            return run
        run.status = "waiting"
        run.result_payload = result_payload or run.result_payload or {}
        self.db.commit()
        self.db.refresh(run)
        return run

    def mark_waiting_operation_run(
        self,
        operation_run_id: int,
        *,
        status: str,
        pending_state: Dict[str, Any],
        trace: Optional[Any] = None,
        output_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkflowOperationRun]:
        operation_run = (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )
        if not operation_run:
            return None
        if operation_run.status in TERMINAL_OPERATION_RUN_STATUSES:
            return operation_run
        if status not in WAITING_OPERATION_RUN_STATUSES:
            raise ValueError(f"Unsupported waiting operation status: {status}")
        operation_run.status = status
        progress = dict(operation_run.progress_payload or {})
        history = list(progress.get("resume_history") or [])
        operation_run.progress_payload = {"pending_state": pending_state, "resume_history": history}
        operation_run.output_payload = output_payload or {"mode": "workflow_waiting", "pending_state": pending_state}
        operation_run.trace = trace
        operation_run.last_heartbeat_at = datetime.utcnow()
        operation_run.finished_at = None
        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def mark_operation_running(self, operation_run_id: int) -> Optional[WorkflowOperationRun]:
        operation_run = (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )
        if not operation_run:
            return None
        if operation_run.status in TERMINAL_OPERATION_RUN_STATUSES:
            return operation_run
        operation_run.status = "running"
        operation_run.error = None
        operation_run.last_heartbeat_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def get_operation_run(self, operation_run_id: int) -> Optional[WorkflowOperationRun]:
        return (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )

    def get_waiting_operation_run(
        self,
        *,
        run_id: int,
        operation_id: int | None = None,
    ) -> Optional[WorkflowOperationRun]:
        q = self.db.query(WorkflowOperationRun).filter(
            WorkflowOperationRun.workflow_run_id == run_id,
            WorkflowOperationRun.status.in_(list(WAITING_OPERATION_RUN_STATUSES)),
        )
        if operation_id is not None:
            q = q.filter(WorkflowOperationRun.workflow_operation_id == operation_id)
        return q.order_by(WorkflowOperationRun.id.desc()).first()

    def mark_operation_cancelled(
            self,
            operation_run_id: int,
            *,
            error: str = "cancelled",
            output_payload: Optional[Dict[str, Any]] = None,
            trace: Optional[Any] = None,
    ) -> Optional[WorkflowOperationRun]:
        operation_run = (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )
        if not operation_run:
            return None

        if operation_run.status in TERMINAL_OPERATION_RUN_STATUSES:
            return operation_run

        operation_run.status = "cancelled"
        operation_run.error = error
        if output_payload is not None:
            operation_run.output_payload = output_payload
        if trace is not None:
            operation_run.trace = trace
        operation_run.progress_payload = self._progress_with_history(operation_run.progress_payload)
        operation_run.finished_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def create_operation_run(
        self,
        *,
        workflow_run_id: int,
        workflow_operation_id: int,
        input_payload: Optional[Dict[str, Any]] = None,
    ) -> WorkflowOperationRun:
        operation_run = WorkflowOperationRun(
            workflow_run_id=workflow_run_id,
            workflow_operation_id=workflow_operation_id,
            status="running",
            input_payload=input_payload or {},
            started_at=datetime.utcnow(),
        )
        self.db.add(operation_run)
        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def finish_operation_run(
            self,
            operation_run_id: int,
            *,
            output_payload: Optional[Dict[str, Any]] = None,
            trace: Optional[Any] = None,
    ) -> Optional[WorkflowOperationRun]:
        operation_run = (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )
        if not operation_run:
            return None

        if operation_run.status in TERMINAL_OPERATION_RUN_STATUSES:
            return operation_run

        operation_run.status = "success"
        operation_run.output_payload = output_payload or {}
        operation_run.progress_payload = self._progress_with_history(operation_run.progress_payload)
        operation_run.trace = trace
        operation_run.finished_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def fail_operation_run(
            self,
            operation_run_id: int,
            *,
            error: str,
            output_payload: Optional[Dict[str, Any]] = None,
            trace: Optional[Any] = None,
    ) -> Optional[WorkflowOperationRun]:
        operation_run = (
            self.db.query(WorkflowOperationRun)
            .filter(WorkflowOperationRun.id == operation_run_id)
            .first()
        )
        if not operation_run:
            return None

        if operation_run.status in TERMINAL_OPERATION_RUN_STATUSES:
            return operation_run

        operation_run.status = "failed"
        operation_run.error = error
        if output_payload is not None:
            operation_run.output_payload = output_payload
        if trace is not None:
            operation_run.trace = trace
        operation_run.progress_payload = self._progress_with_history(operation_run.progress_payload)
        operation_run.finished_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(operation_run)
        return operation_run

    def request_cancel_child_runs(self, parent_run_id: int):
        runs = self.db.query(WorkflowRun).filter(
            WorkflowRun.parent_run_id == parent_run_id,
            WorkflowRun.status.in_(["queued", "running"]),
        ).all()

        now = datetime.utcnow()

        for run in runs:
            run.status = "cancel_requested"

            if hasattr(run, "cancel_requested_at"):
                run.cancel_requested_at = now

        self.db.commit()

        # Recurse after commit so nested children are also marked.
        for run in runs:
            self.request_cancel_child_runs(run.id)

        return runs

    def request_cancel_for_each_sibling_runs(
            self,
            *,
            parent_run_id: int,
            parent_operation_run_id: int,
            except_run_id: int | None = None,
            reason: str = "cancelled because a sibling for_each item failed",
    ):
        query = self.db.query(WorkflowRun).filter(
            WorkflowRun.parent_run_id == parent_run_id,
            WorkflowRun.parent_operation_run_id == parent_operation_run_id,
            WorkflowRun.status.in_(["queued", "running"]),
        )

        if except_run_id is not None:
            query = query.filter(WorkflowRun.id != except_run_id)

        runs = query.all()
        now = datetime.utcnow()

        for run in runs:
            run.status = "cancel_requested"
            run.error = reason

            if hasattr(run, "cancel_requested_at"):
                run.cancel_requested_at = now

        self.db.commit()

        # Also cascade into grandchildren of those sibling child runs.
        for run in runs:
            self.request_cancel_child_runs(run.id)

        return runs

    def request_cancel_run(self, run_id: int):
        run = self.get_run(run_id)
        if not run:
            return None

        if run.status in TERMINAL_RUN_STATUSES:
            return run

        run.status = "cancel_requested"

        if hasattr(run, "cancel_requested_at"):
            run.cancel_requested_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)

        self.request_cancel_child_runs(run_id)

        return run

    def mark_cancelled(self, run_id: int, result_payload: dict | None = None, error: str | None = None):
        run = self.get_run(run_id)
        if not run:
            return None

        if run.status in TERMINAL_RUN_STATUSES:
            return run

        run.status = "cancelled"
        if error:
            run.error = error
        run.finished_at = datetime.utcnow()
        run.result_payload = result_payload or {}

        if hasattr(run, "cancelled_at"):
            run.cancelled_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)
        return run

    def is_cancel_requested(self, run_id: int) -> bool:
        run = self.get_run(run_id)
        return bool(run and run.status == "cancel_requested")

    def exists_scheduled_run(self, *, workflow_id: int, scheduled_for: str) -> bool:
        return (
                self.db.query(WorkflowRun)
                .filter(
                    WorkflowRun.workflow_id == workflow_id,
                    WorkflowRun.trigger_type == "cron",
                    WorkflowRun.input_payload["scheduled_for"].as_string() == scheduled_for,
                )
                .first()
                is not None
        )