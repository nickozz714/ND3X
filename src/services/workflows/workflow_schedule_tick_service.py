from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from component.logging import get_logger
from services.workflows.workflow_factory import WorkflowFactory
from services.workflows.workflow_run_service import WorkflowRunService
from services.workflows.workflow_service import WorkflowService

log = get_logger(__name__)

SessionFactory = Callable[[], Session]


class WorkflowScheduleTickService:
    def __init__(self, *, session_factory: SessionFactory):
        self.session_factory = session_factory

    async def tick_once(self) -> None:
        log.infox("Workflow schedule tick gestart")

        db = self.session_factory()
        try:
            workflow_service = WorkflowService(db)
            workflow_run_service = WorkflowRunService(db)

            factory = WorkflowFactory(
                workflow_service=workflow_service,
                workflow_run_service=workflow_run_service,
            )

            created = factory.tick()

            log.infox(
                "Workflow schedule tick afgerond",
                created_count=len(created or []),
                created_run_ids=[getattr(run, "id", None) for run in (created or [])],
            )
        finally:
            db.close()