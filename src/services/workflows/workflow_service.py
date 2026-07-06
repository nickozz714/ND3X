from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.workflow_repository import WorkflowRepository
from schemas.workflow import WorkflowCreate, WorkflowUpdate

logger = logging.getLogger(__name__)
log = get_logger(__name__)


class WorkflowService:
    def __init__(self, db: Session):
        log.infox(
            "WorkflowService initialiseren",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        self.repository = WorkflowRepository(db)
        log.infox(
            "WorkflowService geïnitialiseerd",
            repository_type=type(self.repository).__name__,
        )

    def get_all(self, skip: int = 0, limit: int = 100, include_disabled: bool = True):
        log.infox(
            "Workflows ophalen gestart",
            skip=skip,
            limit=limit,
            include_disabled=include_disabled,
        )
        result = self.repository.get_all(skip=skip, limit=limit, include_disabled=include_disabled)
        log.infox(
            "Workflows ophalen afgerond",
            skip=skip,
            limit=limit,
            include_disabled=include_disabled,
            result_count=len(result or []),
        )
        return result

    def get_by_id(self, workflow_id: int):
        log.infox(
            "Workflow ophalen op id gestart",
            workflow_id=workflow_id,
        )
        workflow = self.repository.get_by_id(workflow_id)
        if not workflow:
            log.warningx(
                "Workflow niet gevonden op id",
                workflow_id=workflow_id,
            )
            raise HTTPException(status_code=404, detail="Workflow not found")
        log.infox(
            "Workflow ophalen op id afgerond",
            workflow_id=workflow_id,
            workflow_name=getattr(workflow, "name", None),
            is_enabled=getattr(workflow, "is_enabled", None),
            schedule_cron=getattr(workflow, "schedule_cron", None),
        )
        return workflow

    def get_with_operations(self, workflow_id: int):
        log.infox(
            "Workflow met operations ophalen gestart",
            workflow_id=workflow_id,
        )
        workflow = self.repository.get_with_operations(workflow_id)
        if not workflow:
            log.warningx(
                "Workflow met operations niet gevonden",
                workflow_id=workflow_id,
            )
            raise HTTPException(status_code=404, detail="Workflow not found")
        log.infox(
            "Workflow met operations ophalen afgerond",
            workflow_id=workflow_id,
            workflow_name=getattr(workflow, "name", None),
            is_enabled=getattr(workflow, "is_enabled", None),
            operation_count=len(getattr(workflow, "operations", []) or []),
        )
        return workflow

    def create(self, data: WorkflowCreate):
        log.infox(
            "Workflow aanmaken gestart",
            workflow_name=getattr(data, "name", None),
            is_enabled=getattr(data, "is_enabled", None),
            schedule_cron=getattr(data, "schedule_cron", None),
        )
        existing = self.repository.get_by_name(data.name)
        if existing:
            log.warningx(
                "Workflow aanmaken geblokkeerd: naam bestaat al",
                workflow_name=data.name,
                existing_id=getattr(existing, "id", None),
            )
            raise HTTPException(status_code=409, detail="Workflow name already exists")
        workflow = self.repository.create(data)
        log.infox(
            "Workflow aanmaken afgerond",
            workflow_id=getattr(workflow, "id", None),
            workflow_name=getattr(workflow, "name", None),
            is_enabled=getattr(workflow, "is_enabled", None),
            schedule_cron=getattr(workflow, "schedule_cron", None),
        )
        return workflow

    def update(self, workflow_id: int, data: WorkflowUpdate):
        log.infox(
            "Workflow bijwerken gestart",
            workflow_id=workflow_id,
            workflow_name=getattr(data, "name", None),
            is_enabled=getattr(data, "is_enabled", None),
            schedule_cron=getattr(data, "schedule_cron", None),
        )
        workflow = self.repository.update(workflow_id, data)
        if not workflow:
            log.warningx(
                "Workflow niet gevonden voor update",
                workflow_id=workflow_id,
            )
            raise HTTPException(status_code=404, detail="Workflow not found")
        log.infox(
            "Workflow bijwerken afgerond",
            workflow_id=workflow_id,
            workflow_name=getattr(workflow, "name", None),
            is_enabled=getattr(workflow, "is_enabled", None),
            schedule_cron=getattr(workflow, "schedule_cron", None),
        )
        return workflow

    def delete(self, workflow_id: int):
        log.infox(
            "Workflow verwijderen gestart",
            workflow_id=workflow_id,
        )
        if not self.repository.delete(workflow_id):
            log.warningx(
                "Workflow niet gevonden voor delete",
                workflow_id=workflow_id,
            )
            raise HTTPException(status_code=404, detail="Workflow not found")
        log.infox(
            "Workflow verwijderen afgerond",
            workflow_id=workflow_id,
        )
        return {"detail": "Workflow deleted"}

    def get_enabled_scheduled(self):
        log.infox("Enabled scheduled workflows ophalen gestart")
        result = self.repository.get_enabled_scheduled()
        log.infox(
            "Enabled scheduled workflows ophalen afgerond",
            result_count=len(result or []),
            workflow_ids=[getattr(workflow, "id", None) for workflow in (result or [])],
        )
        return result