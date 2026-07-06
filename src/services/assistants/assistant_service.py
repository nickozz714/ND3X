import logging
from datetime import datetime

from sqlalchemy.orm import Session
from repository.assistant_repository import AssistantRepository
from schemas.assistant import AssistantCreate, AssistantUpdate
from fastapi import HTTPException
from component.logging import get_logger

logger = logging.getLogger(__name__)
log = get_logger(__name__)




def _is_protected_assistant_obj(assistant) -> bool:
    at = (getattr(assistant, "assistant_type", "") or "").lower()
    nm = (getattr(assistant, "name", "") or "")
    return at in {"router", "final_answer", "answer"} or nm in {"RouterAssistant", "AnswerAssistant"}


def _is_protected_assistant_payload(data) -> bool:
    at = (getattr(data, "assistant_type", "") or "").lower()
    nm = (getattr(data, "name", "") or "")
    return at in {"router", "final_answer", "answer"} or nm in {"RouterAssistant", "AnswerAssistant"}


def _enforce_code_authoritative_fields(data, assistant_type: str) -> None:
    """Forceer code-authoritative schema/instructie op een create/update payload.

    Response schemas (router/planner/final_answer) en instructies (router/
    final_answer) staan in code, niet in de database. Client-waarden voor deze
    velden worden genegeerd en overschreven met de hardcoded specs.
    """
    from services.assistants.runtime.system_assistants import (
        schema_for_type,
        instruction_override_for_type,
    )

    schema = schema_for_type(assistant_type or "")
    if schema is not None:
        data.schema = schema
        try:
            data.__pydantic_fields_set__.add("schema")
        except Exception:  # noqa: BLE001 — niet-Pydantic of ouder model
            pass

    instruction = instruction_override_for_type(assistant_type or "")
    if instruction is not None:
        data.instruction = instruction
        try:
            data.__pydantic_fields_set__.add("instruction")
        except Exception:  # noqa: BLE001
            pass

class AssistantService:
    def __init__(self, db: Session):
        log.debugx(
            "AssistantService initialiseren",
            has_db_session=db is not None,
        )
        self.repository = AssistantRepository(db)
        log.debugx("AssistantRepository gekoppeld aan AssistantService")

    def get_all(self, skip: int = 0, limit: int = 100):
        logger.debug("Service: get_all assistant")
        log.infox(
            "Assistants ophalen gestart",
            skip=skip,
            limit=limit,
        )
        result = self.repository.get_all(skip=skip, limit=limit)
        log.infox(
            "Assistants ophalen afgerond",
            skip=skip,
            limit=limit,
            count=len(result) if result is not None else None,
        )
        return result

    def get_by_id(self, id: int):
        log.infox(
            "Assistant ophalen op ID gestart",
            assistant_id=id,
        )
        obj = self.repository.get_by_id(id)
        if not obj:
            logger.warning("Assistant not found: id=%s", id)
            log.warningx(
                "Assistant niet gevonden op ID",
                assistant_id=id,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")
        log.infox(
            "Assistant ophalen op ID afgerond",
            assistant_id=id,
            found=True,
            name=getattr(obj, "name", None),
            slug=getattr(obj, "slug", None),
        )
        return obj

    def get_by_name(self, name: str):
        log.infox(
            "Assistant ophalen op naam gestart",
            name=name,
        )
        obj = self.repository.get_by_name(name)
        if not obj:
            logger.warning("Assistant not found: name=%s", name)
            log.warningx(
                "Assistant niet gevonden op naam",
                name=name,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")
        log.infox(
            "Assistant ophalen op naam afgerond",
            assistant_id=getattr(obj, "id", None),
            name=name,
            slug=getattr(obj, "slug", None),
        )
        return obj


    def get_with_relations(self, id: int):
        log.infox(
            "Assistant met relaties ophalen gestart",
            assistant_id=id,
        )
        obj = self.repository.get_with_relations(id)
        if not obj:
            logger.warning("Assistant not found: id=%s", id)
            log.warningx(
                "Assistant met relaties niet gevonden",
                assistant_id=id,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")
        log.infox(
            "Assistant met relaties ophalen afgerond",
            assistant_id=id,
            found=True,
            name=getattr(obj, "name", None),
            slug=getattr(obj, "slug", None),
            tools_count=len(getattr(obj, "tools", []) or []) if hasattr(obj, "tools") else None,
        )
        return obj

    def get_all_with_relations(self, skip: int = 0, limit: int = 100):
        logger.debug("Service: get_all_with_relations assistant")
        log.infox(
            "Assistants met relaties ophalen gestart",
            skip=skip,
            limit=limit,
        )
        result = self.repository.get_all_with_relations(skip=skip, limit=limit)
        log.infox(
            "Assistants met relaties ophalen afgerond",
            skip=skip,
            limit=limit,
            count=len(result) if result is not None else None,
        )
        return result

    def create(self, data: AssistantCreate, user=None):
        if _is_protected_assistant_payload(data):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        logger.info("Service: creating assistant")
        log.infox(
            "Assistant aanmaken gestart",
            name=getattr(data, "name", None),
            slug=getattr(data, "slug", None),
            is_active=getattr(data, "is_active", None),
        )
        _enforce_code_authoritative_fields(data, getattr(data, "assistant_type", "") or "")
        data.created_at = datetime.utcnow() # Set date
        data.updated_at = datetime.utcnow()
        log.debugx(
            "Assistant timestamps gezet",
            name=getattr(data, "name", None),
            slug=getattr(data, "slug", None),
            created_at=data.created_at,
            updated_at=data.updated_at,
        )
        result = self.repository.create(data)
        log.infox(
            "Assistant aanmaken afgerond",
            assistant_id=getattr(result, "id", None),
            name=getattr(result, "name", None),
            slug=getattr(result, "slug", None),
        )
        return result

    def update(self, id: int, data: AssistantUpdate, user=None):
        existing = self.get_by_id(id)
        if _is_protected_assistant_obj(existing) or _is_protected_assistant_payload(data):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        effective_type = (getattr(data, "assistant_type", None) or getattr(existing, "assistant_type", "") or "")
        _enforce_code_authoritative_fields(data, effective_type)
        log.infox(
            "Assistant bijwerken gestart",
            assistant_id=id,
            name=getattr(data, "name", None),
            slug=getattr(data, "slug", None),
            is_active=getattr(data, "is_active", None),
        )
        obj = self.repository.update(id, data)
        if not obj:
            logger.warning("Assistant not found for update: id=%s", id)
            log.warningx(
                "Assistant niet gevonden voor update",
                assistant_id=id,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")
        log.infox(
            "Assistant bijwerken afgerond",
            assistant_id=id,
            result_id=getattr(obj, "id", None),
            name=getattr(obj, "name", None),
            slug=getattr(obj, "slug", None),
        )
        return obj

    def delete(self, id: int, user=None):
        existing = self.get_by_id(id)
        if _is_protected_assistant_obj(existing):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        log.infox(
            "Assistant verwijderen gestart",
            assistant_id=id,
        )
        success = self.repository.delete(id)
        if not success:
            logger.warning("Assistant not found for delete: id=%s", id)
            log.warningx(
                "Assistant niet gevonden voor verwijderen",
                assistant_id=id,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")
        log.infox(
            "Assistant verwijderen afgerond",
            assistant_id=id,
            success=success,
        )
        return {"detail": "Assistant deleted"}

    def get_tool_by_name_with_server(self, tool_name):
        log.debugx(
            "Tool ophalen op naam met server gestart",
            tool_name=tool_name,
        )
