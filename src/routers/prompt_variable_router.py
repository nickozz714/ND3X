from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from component.logging import get_logger
from db.database import get_db
from schemas.prompt_variable import (
    PromptVariableCreate,
    PromptVariableRead,
    PromptVariableUpdate,
)
from services.workflows.prompt_variable_service import PromptVariableService


log = get_logger(__name__)

router = APIRouter(prefix="/prompt-variables", tags=["prompt-variables"])


def get_prompt_variable_service(db: Session = Depends(get_db)) -> PromptVariableService:
    log.debugx("PromptVariableService dependency aanmaken")
    return PromptVariableService(db)


@router.post("", response_model=PromptVariableRead)
def create_prompt_variable(
    data: PromptVariableCreate,
    service: PromptVariableService = Depends(get_prompt_variable_service),
):
    log.infox(
        "Prompt variable aanmaken gestart",
        key=getattr(data, "key", None),
        name=getattr(data, "name", None),
        workflow_id=getattr(data, "workflow_id", None),
        operation_id=getattr(data, "operation_id", None),
    )
    result = service.create(data)
    log.infox(
        "Prompt variable aanmaken afgerond",
        prompt_variable_id=getattr(result, "id", None),
        key=getattr(result, "key", None),
        name=getattr(result, "name", None),
        workflow_id=getattr(result, "workflow_id", None),
        operation_id=getattr(result, "operation_id", None),
    )
    return result


@router.get("", response_model=List[PromptVariableRead])
def list_prompt_variables(
    skip: int = 0,
    limit: int = 100,
    service: PromptVariableService = Depends(get_prompt_variable_service),
):
    log.infox(
        "Prompt variables ophalen gestart",
        skip=skip,
        limit=limit,
    )
    result = service.get_all(skip=skip, limit=limit)
    log.infox(
        "Prompt variables ophalen afgerond",
        skip=skip,
        limit=limit,
        count=len(result) if result is not None else None,
    )
    return result


@router.get("/{prompt_variable_id}", response_model=PromptVariableRead)
def get_prompt_variable(
    prompt_variable_id: int,
    service: PromptVariableService = Depends(get_prompt_variable_service),
):
    log.infox(
        "Prompt variable ophalen gestart",
        prompt_variable_id=prompt_variable_id,
    )
    result = service.get_by_id(prompt_variable_id)
    log.infox(
        "Prompt variable ophalen afgerond",
        prompt_variable_id=prompt_variable_id,
        found=result is not None,
        key=getattr(result, "key", None),
        name=getattr(result, "name", None),
    )
    return result


@router.put("/{prompt_variable_id}", response_model=PromptVariableRead)
def update_prompt_variable(
    prompt_variable_id: int,
    data: PromptVariableUpdate,
    service: PromptVariableService = Depends(get_prompt_variable_service),
):
    log.infox(
        "Prompt variable bijwerken gestart",
        prompt_variable_id=prompt_variable_id,
        key=getattr(data, "key", None),
        name=getattr(data, "name", None),
        workflow_id=getattr(data, "workflow_id", None),
        operation_id=getattr(data, "operation_id", None),
    )
    result = service.update(prompt_variable_id, data)
    log.infox(
        "Prompt variable bijwerken afgerond",
        prompt_variable_id=prompt_variable_id,
        result_id=getattr(result, "id", None),
        key=getattr(result, "key", None),
        name=getattr(result, "name", None),
        workflow_id=getattr(result, "workflow_id", None),
        operation_id=getattr(result, "operation_id", None),
    )
    return result


@router.delete("/{prompt_variable_id}")
def delete_prompt_variable(
    prompt_variable_id: int,
    service: PromptVariableService = Depends(get_prompt_variable_service),
):
    log.infox(
        "Prompt variable verwijderen gestart",
        prompt_variable_id=prompt_variable_id,
    )
    result = service.delete(prompt_variable_id)
    log.infox(
        "Prompt variable verwijderen afgerond",
        prompt_variable_id=prompt_variable_id,
    )
    return result