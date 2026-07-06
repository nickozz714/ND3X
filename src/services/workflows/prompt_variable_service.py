from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.prompt_variable_repository import PromptVariableRepository
from schemas.prompt_variable import PromptVariableCreate, PromptVariableUpdate


log = get_logger(__name__)


class PromptVariableService:
    def __init__(self, db: Session):
        log.infox(
            "PromptVariableService initialiseren",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        self.repository = PromptVariableRepository(db)
        log.infox(
            "PromptVariableService geïnitialiseerd",
            repository_type=type(self.repository).__name__,
        )

    def get_all(self, skip: int = 0, limit: int = 100):
        log.infox(
            "Prompt variables ophalen gestart",
            skip=skip,
            limit=limit,
        )
        result = self.repository.get_all(skip=skip, limit=limit)
        log.infox(
            "Prompt variables ophalen afgerond",
            skip=skip,
            limit=limit,
            result_count=len(result or []),
        )
        return result

    def get_by_id(self, prompt_variable_id: int):
        log.infox(
            "Prompt variable ophalen op id gestart",
            prompt_variable_id=prompt_variable_id,
        )

        item = self.repository.get_by_id(prompt_variable_id)

        if not item:
            log.warningx(
                "Prompt variable niet gevonden op id",
                prompt_variable_id=prompt_variable_id,
            )
            raise HTTPException(status_code=404, detail="Prompt variable not found")

        log.infox(
            "Prompt variable ophalen op id afgerond",
            prompt_variable_id=prompt_variable_id,
            token=getattr(item, "token", None),
            is_enabled=getattr(item, "is_enabled", None),
        )
        return item

    def create(self, data: PromptVariableCreate):
        log.infox(
            "Prompt variable aanmaken gestart",
            token=getattr(data, "token", None),
            is_enabled=getattr(data, "is_enabled", None),
            timeout_ms=getattr(data, "timeout_ms", None),
        )

        existing = self.repository.get_by_token(data.token)

        if existing:
            log.warningx(
                "Prompt variable aanmaken geblokkeerd: token bestaat al",
                token=data.token,
                existing_id=getattr(existing, "id", None),
            )
            raise HTTPException(status_code=409, detail="Prompt variable token already exists")

        item = self.repository.create(data)

        log.infox(
            "Prompt variable aanmaken afgerond",
            prompt_variable_id=getattr(item, "id", None),
            token=getattr(item, "token", None),
            is_enabled=getattr(item, "is_enabled", None),
        )
        return item

    def update(self, prompt_variable_id: int, data: PromptVariableUpdate):
        log.infox(
            "Prompt variable bijwerken gestart",
            prompt_variable_id=prompt_variable_id,
            token=getattr(data, "token", None),
            is_enabled=getattr(data, "is_enabled", None),
            timeout_ms=getattr(data, "timeout_ms", None),
        )

        if data.token:
            log.debugx(
                "Prompt variable token conflict controleren",
                prompt_variable_id=prompt_variable_id,
                token=data.token,
            )
            existing = self.repository.get_by_token(data.token)

            if existing and existing.id != prompt_variable_id:
                log.warningx(
                    "Prompt variable bijwerken geblokkeerd: token bestaat al bij ander record",
                    prompt_variable_id=prompt_variable_id,
                    token=data.token,
                    existing_id=getattr(existing, "id", None),
                )
                raise HTTPException(status_code=409, detail="Prompt variable token already exists")

        item = self.repository.update(prompt_variable_id, data)

        if not item:
            log.warningx(
                "Prompt variable niet gevonden voor update",
                prompt_variable_id=prompt_variable_id,
            )
            raise HTTPException(status_code=404, detail="Prompt variable not found")

        log.infox(
            "Prompt variable bijwerken afgerond",
            prompt_variable_id=prompt_variable_id,
            token=getattr(item, "token", None),
            is_enabled=getattr(item, "is_enabled", None),
        )
        return item

    def delete(self, prompt_variable_id: int):
        log.infox(
            "Prompt variable verwijderen gestart",
            prompt_variable_id=prompt_variable_id,
        )

        if not self.repository.delete(prompt_variable_id):
            log.warningx(
                "Prompt variable niet gevonden voor delete",
                prompt_variable_id=prompt_variable_id,
            )
            raise HTTPException(status_code=404, detail="Prompt variable not found")

        log.infox(
            "Prompt variable verwijderen afgerond",
            prompt_variable_id=prompt_variable_id,
        )
        return {"detail": "Prompt variable deleted"}