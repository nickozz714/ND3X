from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.workflow_repository import WorkflowRepository
from repository.workflow_run_repository import WorkflowRunRepository
from services.workflows.workflow_execution_provider import WorkflowExecutionProvider


log = get_logger(__name__)


class WorkflowRunService:
    def __init__(self, db: Session):
        log.infox(
            "WorkflowRunService initialiseren",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        self.workflow_repository = WorkflowRepository(db)
        self.run_repository = WorkflowRunRepository(db)
        log.infox(
            "WorkflowRunService geïnitialiseerd",
            workflow_repository_type=type(self.workflow_repository).__name__,
            run_repository_type=type(self.run_repository).__name__,
        )

    def enqueue_run(self, *, workflow_id: int, trigger_type: str, input_payload: Dict[str, Any]):
        log.infox(
            "Workflow run enqueue gestart",
            workflow_id=workflow_id,
            trigger_type=trigger_type,
            input_payload_keys=list((input_payload or {}).keys()) if isinstance(input_payload or {}, dict) else None,
        )
        workflow = self.workflow_repository.get_by_id(workflow_id)
        if not workflow:
            log.warningx(
                "Workflow run enqueue mislukt: workflow niet gevonden",
                workflow_id=workflow_id,
                trigger_type=trigger_type,
            )
            raise HTTPException(status_code=404, detail="Workflow not found")
        if not workflow.is_enabled and trigger_type == "cron":
            log.warningx(
                "Workflow run enqueue geblokkeerd: workflow disabled voor cron",
                workflow_id=workflow_id,
                workflow_name=getattr(workflow, "name", None),
                trigger_type=trigger_type,
                is_enabled=getattr(workflow, "is_enabled", None),
            )
            raise HTTPException(status_code=409, detail="Workflow is disabled")
        run = self.run_repository.create_run(
            workflow_id=workflow_id,
            trigger_type=trigger_type,
            input_payload=input_payload or {},
        )
        log.infox(
            "Workflow run enqueue afgerond",
            workflow_id=workflow_id,
            workflow_name=getattr(workflow, "name", None),
            trigger_type=trigger_type,
            workflow_run_id=getattr(run, "id", None),
            status=getattr(run, "status", None),
        )
        return run

    def get_run(self, run_id: int):
        log.infox(
            "Workflow run ophalen gestart",
            run_id=run_id,
        )
        run = self.run_repository.get_run(run_id)
        if not run:
            log.warningx(
                "Workflow run niet gevonden",
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail="Workflow run not found")
        log.infox(
            "Workflow run ophalen afgerond",
            run_id=run_id,
            workflow_id=getattr(run, "workflow_id", None),
            status=getattr(run, "status", None),
            trigger_type=getattr(run, "trigger_type", None),
            parent_run_id=getattr(run, "parent_run_id", None),
        )
        return run

    def cancel_run(self, run_id: int):
        """Request cancellation of a workflow run.

        Sets the run to `cancel_requested`; the executor's cancellation
        checkpoints (`_raise_if_cancel_requested`) then stop the run and mark it
        `cancelled`. A queued run is cancelled as soon as the worker picks it up.
        Already-terminal runs are returned unchanged.
        """
        log.infox("Workflow run annuleren (service) gestart", run_id=run_id)
        run = self.get_run(run_id)  # raises 404 when missing
        if run.status in {"success", "failed", "cancelled"}:
            log.infox(
                "Workflow run al terminal, annuleren overgeslagen",
                run_id=run_id,
                status=run.status,
            )
            return run
        cancelled = self.run_repository.request_cancel_run(run_id)
        log.infox(
            "Workflow run annuleren (service) afgerond",
            run_id=run_id,
            status=getattr(cancelled, "status", None),
        )
        return cancelled or run

    def get_run_with_operations(self, run_id: int):
        log.infox(
            "Workflow run met operations ophalen gestart",
            run_id=run_id,
        )
        run = self.run_repository.get_run_with_operations(run_id)
        if not run:
            log.warningx(
                "Workflow run met operations niet gevonden",
                run_id=run_id,
            )
            raise HTTPException(status_code=404, detail="Workflow run not found")
        log.infox(
            "Workflow run met operations ophalen afgerond",
            run_id=run_id,
            workflow_id=getattr(run, "workflow_id", None),
            status=getattr(run, "status", None),
            operation_run_count=len(getattr(run, "operation_runs", []) or []),
        )
        return run

    def list_runs_for_workflow(self, workflow_id: int, skip: int = 0, limit: int = 100):
        log.infox(
            "Workflow runs voor workflow ophalen gestart",
            workflow_id=workflow_id,
            skip=skip,
            limit=limit,
        )
        runs = self.run_repository.list_runs_for_workflow(workflow_id, skip=skip, limit=limit)
        log.infox(
            "Workflow runs voor workflow ophalen afgerond",
            workflow_id=workflow_id,
            skip=skip,
            limit=limit,
            run_count=len(runs or []),
        )
        return runs


    def _parse_iso(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value)
            if text.endswith("Z"):
                text = text[:-1]
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _compact_preview(self, value: Any, limit: int = 300) -> str:
        text = "" if value is None else str(value)
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _is_pending_expired(self, pending: Dict[str, Any]) -> bool:
        expires_at = self._parse_iso((pending or {}).get("expires_at"))
        return bool(expires_at and datetime.utcnow() >= expires_at)

    def _append_timeout_trace(self, op_run, pending: Dict[str, Any], status: str) -> list:
        trace = list(op_run.trace or [])
        trace.append({
            "type": "workflow_waiting_timeout_expired",
            "level": "warn",
            "summary": "workflow waiting timeout expired",
            "data": {
                "operation_id": op_run.workflow_operation_id,
                "expires_at": pending.get("expires_at"),
                "on_timeout": ((pending.get("waiting_policy") or {}).get("on_timeout") or "fail"),
                "status": status,
            },
        })
        return trace

    def _timeout_output(self, pending: Dict[str, Any], status: str) -> Dict[str, Any]:
        return {
            "status": status,
            "type": "workflow_waiting_timeout_expired",
            "reason": "Workflow waiting timeout expired",
            "pending_type": pending.get("type"),
            "expires_at": pending.get("expires_at"),
        }

    def _enforce_waiting_timeout(self, run, op_run) -> bool:
        pending = ((op_run.progress_payload or {}).get("pending_state") or {})
        if not self._is_pending_expired(pending):
            return False
        on_timeout = str(((pending.get("waiting_policy") or {}).get("on_timeout") or "fail")).strip().lower()
        if on_timeout not in {"fail", "cancel", "keep_waiting"}:
            on_timeout = "fail"
        if on_timeout == "keep_waiting":
            return False
        error = f"Workflow waiting timeout expired for operation {op_run.workflow_operation_id}"
        output = self._timeout_output(pending, "cancelled" if on_timeout == "cancel" else "failed")
        trace = self._append_timeout_trace(op_run, pending, output["status"])
        if on_timeout == "cancel":
            self.run_repository.mark_operation_cancelled(op_run.id, error=error, output_payload=output, trace=trace)
            self.run_repository.mark_cancelled(run.id, result_payload=output, error=error)
        else:
            self.run_repository.fail_operation_run(op_run.id, error=error, output_payload=output, trace=trace)
            self.run_repository.mark_failed(run.id, error=error, result_payload=output)
        return True

    def _public_pending_state(self, pending: Dict[str, Any]) -> Dict[str, Any]:
        pending = dict(pending or {})
        allowed = {
            "type",
            "operation_id",
            "operation_name",
            "assistant_id",
            "assistant_name",
            "skill_names",
            "question",
            "context_summary",
            "tool",
            "tool_id",
            "risk_level",
            "message",
            "confirmation_prompt",
            "display",
            "policy_decision",
            "tool_call_hash",
            "created_at",
            "expires_at",
            "waiting_policy",
        }
        public = {k: v for k, v in pending.items() if k in allowed}
        display = public.get("display")
        if isinstance(display, dict):
            public["display"] = {
                "command": self._compact_preview(display.get("command"), 300),
                "working_dir": display.get("working_dir"),
                "timeout": display.get("timeout"),
            }
        public["expired"] = self._is_pending_expired(pending)
        return public


    def get_pending(self, run_id: int, operation_id: int | None = None) -> Dict[str, Any]:
        run = self.get_run(run_id)
        op_run = self.run_repository.get_waiting_operation_run(run_id=run_id, operation_id=operation_id)
        if not op_run:
            raise HTTPException(status_code=404, detail="No pending workflow operation found")
        if self._enforce_waiting_timeout(run, op_run):
            raise HTTPException(status_code=409, detail="Workflow waiting timeout expired")
        op_run = self.run_repository.get_operation_run(op_run.id)
        pending = ((op_run.progress_payload or {}).get("pending_state") or {})
        history = list((op_run.progress_payload or {}).get("resume_history") or [])
        public_pending = self._public_pending_state(pending)
        operation_name = public_pending.get("operation_name") or getattr(getattr(op_run, "operation", None), "name", None)
        return {
            "run_id": run.id,
            "run_status": run.status,
            "operation_run_id": op_run.id,
            "operation_id": op_run.workflow_operation_id,
            "operation_name": operation_name,
            "status": op_run.status,
            "operation_status": op_run.status,
            "pending": public_pending,
            "created_at": public_pending.get("created_at"),
            "expires_at": public_pending.get("expires_at"),
            "expired": public_pending.get("expired"),
            "resume_history": history,
        }

    async def resume_operation(self, *, run_id: int, operation_id: int, payload: Dict[str, Any], resume_by: Any = None) -> Dict[str, Any]:
        run = self.get_run(run_id)
        if run.status in {"success", "failed", "cancelled", "cancel_requested"}:
            raise HTTPException(status_code=409, detail=f"Workflow run cannot be resumed from status={run.status}")
        op_run = self.run_repository.get_waiting_operation_run(run_id=run_id, operation_id=operation_id)
        if not op_run:
            raise HTTPException(status_code=409, detail="No pending workflow operation found")
        if self._enforce_waiting_timeout(run, op_run):
            raise HTTPException(status_code=409, detail="Workflow waiting timeout expired")
        try:
            executor = WorkflowExecutionProvider(db=self.run_repository.db).build_executor()
            return await executor.resume_waiting_operation(
                run_id=run_id,
                operation_id=operation_id,
                resume=payload or {},
                resume_by=resume_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    def exists_scheduled_run(self, *, workflow_id: int, scheduled_for: str) -> bool:
        log.debugx(
            "Scheduled workflow run bestaan controleren gestart",
            workflow_id=workflow_id,
            scheduled_for=scheduled_for,
        )
        result = self.run_repository.exists_scheduled_run(
            workflow_id=workflow_id,
            scheduled_for=scheduled_for,
        )
        log.debugx(
            "Scheduled workflow run bestaan controleren afgerond",
            workflow_id=workflow_id,
            scheduled_for=scheduled_for,
            exists=result,
        )
        return result