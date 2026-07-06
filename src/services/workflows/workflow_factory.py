from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from croniter import croniter

from component.logging import get_logger
from services.workflows.workflow_run_service import WorkflowRunService
from services.workflows.workflow_service import WorkflowService

from zoneinfo import ZoneInfo

log = get_logger(__name__)

APP_TIMEZONE = ZoneInfo("Europe/Amsterdam")

class WorkflowFactory:
    """Creates workflow runs from manual triggers or due cron schedules.

    This class intentionally does not execute workflows. A worker should pick up
    queued runs and pass them to WorkflowExecutor, keeping HTTP requests fast.
    """

    def __init__(self, *, workflow_service: WorkflowService, workflow_run_service: WorkflowRunService):
        log.infox(
            "WorkflowFactory initialiseren",
            has_workflow_service=workflow_service is not None,
            has_workflow_run_service=workflow_run_service is not None,
            workflow_service_type=type(workflow_service).__name__,
            workflow_run_service_type=type(workflow_run_service).__name__,
            app_timezone=str(APP_TIMEZONE),
        )
        self.workflow_service = workflow_service
        self.workflow_run_service = workflow_run_service
        log.infox("WorkflowFactory geïnitialiseerd")

    def trigger_manual(self, *, workflow_id: int, input_payload: Optional[Dict[str, Any]] = None):
        log.infox(
            "Manual workflow trigger gestart",
            workflow_id=workflow_id,
            has_input_payload=input_payload is not None,
            input_payload_keys=list((input_payload or {}).keys()) if isinstance(input_payload or {}, dict) else None,
        )
        run = self.workflow_run_service.enqueue_run(
            workflow_id=workflow_id,
            trigger_type="manual",
            input_payload=input_payload or {},
        )
        log.infox(
            "Manual workflow trigger afgerond",
            workflow_id=workflow_id,
            workflow_run_id=getattr(run, "id", None),
            trigger_type=getattr(run, "trigger_type", None),
            status=getattr(run, "status", None),
        )
        return run

    def tick(self, *, now=None, lookback_seconds=60):
        log.infox(
            "WorkflowFactory scheduler tick gestart",
            provided_now=now is not None,
            lookback_seconds=lookback_seconds,
            app_timezone=str(APP_TIMEZONE),
        )

        now = now or datetime.now(APP_TIMEZONE)

        log.debugx(
            "WorkflowFactory scheduler tick tijd bepaald",
            now=now.isoformat() if hasattr(now, "isoformat") else str(now),
            lookback_seconds=lookback_seconds,
        )

        created = []

        workflows = self.workflow_service.get_enabled_scheduled()

        log.infox(
            "Enabled scheduled workflows opgehaald",
            workflow_count=len(workflows or []),
            now=now.isoformat() if hasattr(now, "isoformat") else str(now),
        )

        for workflow in workflows:
            log.debugx(
                "Scheduled workflow controleren",
                workflow_id=getattr(workflow, "id", None),
                workflow_name=getattr(workflow, "name", None),
                schedule_cron=getattr(workflow, "schedule_cron", None),
            )

            if not workflow.schedule_cron:
                log.debugx(
                    "Scheduled workflow overgeslagen: schedule_cron ontbreekt",
                    workflow_id=getattr(workflow, "id", None),
                    workflow_name=getattr(workflow, "name", None),
                )
                continue

            itr = croniter(workflow.schedule_cron, now)
            previous_due = itr.get_prev(datetime)

            delta = (now - previous_due).total_seconds()

            log.debugx(
                "Scheduled workflow due tijd berekend",
                workflow_id=getattr(workflow, "id", None),
                workflow_name=getattr(workflow, "name", None),
                schedule_cron=workflow.schedule_cron,
                previous_due=previous_due.isoformat() if hasattr(previous_due, "isoformat") else str(previous_due),
                delta_seconds=delta,
                lookback_seconds=lookback_seconds,
            )

            if 0 <= delta <= lookback_seconds:
                scheduled_for = previous_due.isoformat()

                log.infox(
                    "Scheduled workflow valt binnen lookback window",
                    workflow_id=workflow.id,
                    workflow_name=getattr(workflow, "name", None),
                    scheduled_for=scheduled_for,
                    delta_seconds=delta,
                )

                if self.workflow_run_service.exists_scheduled_run(
                        workflow_id=workflow.id,
                        scheduled_for=scheduled_for,
                ):
                    log.infox(
                        "Scheduled workflow run bestaat al, overslaan",
                        workflow_id=workflow.id,
                        workflow_name=getattr(workflow, "name", None),
                        scheduled_for=scheduled_for,
                    )
                    continue

                run = self.workflow_run_service.enqueue_run(
                    workflow_id=workflow.id,
                    trigger_type="cron",
                    input_payload={"scheduled_for": scheduled_for},
                )
                created.append(run)

                log.infox(
                    "Scheduled workflow run aangemaakt",
                    workflow_id=workflow.id,
                    workflow_name=getattr(workflow, "name", None),
                    workflow_run_id=getattr(run, "id", None),
                    scheduled_for=scheduled_for,
                    trigger_type=getattr(run, "trigger_type", None),
                    status=getattr(run, "status", None),
                )
            else:
                log.debugx(
                    "Scheduled workflow niet due binnen lookback window",
                    workflow_id=getattr(workflow, "id", None),
                    workflow_name=getattr(workflow, "name", None),
                    delta_seconds=delta,
                    lookback_seconds=lookback_seconds,
                )

        log.infox(
            "WorkflowFactory scheduler tick afgerond",
            created_count=len(created),
            created_run_ids=[getattr(run, "id", None) for run in created],
        )
        return created