from fastapi import APIRouter, Depends, status
from authentication.dependencies import require_user
from services.authz_service import assert_expert_role
from sqlalchemy.orm import Session

from component.logging import get_logger
from db.database import get_db
from schemas.assistant import (
    AssistantCreate,
    AssistantResponse,
    AssistantUpdate,
    AssistantWithRelations,
    AssistantToolMiniResponse, AssistantSkillMiniResponse,
)
from services.assistants.assistant_service import AssistantService
from services.assistants.assistant_tool_service import AssistantToolService
from services.assistants.skill_service import SkillService

log = get_logger(__name__)

router = APIRouter(prefix="/assistants", tags=["Assistant"])


def get_service(db: Session = Depends(get_db)) -> AssistantService:
    log.debugx("AssistantService dependency aanmaken")
    return AssistantService(db)


def get_assistant_tool_service(db: Session = Depends(get_db)) -> AssistantToolService:
    log.debugx("AssistantToolService dependency aanmaken")
    return AssistantToolService(db)

def get_skill_service(db: Session = Depends(get_db)) -> SkillService:
    log.debugx("SkillService dependency aanmaken")
    return SkillService(db)

import pathlib as _pathlib
from fastapi import HTTPException

# The single agent's instruction lives in editable repo markdown files (source of truth).
# There is a shared BASE instruction plus per-flow blocks: the effective system prompt is
# Base + (Chat block on chat turns | Workflow block on workflow runs). The UI edits each
# via the endpoints below (?flow=base|chat|workflow; default base for back-compat).
_SPEC_DIR = (
    _pathlib.Path(__file__).resolve().parents[1]
    / "services" / "assistants" / "runtime" / "system_specs"
)
_INSTRUCTION_FILES = {
    "base": "agent.instruction.md",
    "chat": "agent.instruction.chat.md",
    "workflow": "agent.instruction.workflow.md",
}


def _instruction_path(flow: str) -> _pathlib.Path:
    name = _INSTRUCTION_FILES.get((flow or "base").strip().lower())
    if not name:
        raise HTTPException(status_code=400, detail="flow must be base, chat or workflow")
    return _SPEC_DIR / name


@router.get("/agent/instruction")
def get_agent_instruction(flow: str = "base", user=Depends(require_user)):
    try:
        text = _instruction_path(flow).read_text(encoding="utf-8")
    except Exception:
        text = ""
    return {"flow": flow, "instruction": text}


@router.put("/agent/instruction")
def put_agent_instruction(body: dict, flow: str = "base", user=Depends(require_user)):
    assert_expert_role(user)
    instruction = (body or {}).get("instruction")
    if not isinstance(instruction, str):
        raise HTTPException(status_code=400, detail="instruction (string) is required")
    # Base is the always-on identity/tone and must be non-empty; the per-flow blocks are
    # optional (an empty block simply adds nothing for that flow).
    if (flow or "base").strip().lower() == "base" and not instruction.strip():
        raise HTTPException(status_code=400, detail="base instruction must be non-empty")
    _instruction_path(flow).write_text(instruction, encoding="utf-8")
    log.infox("Agent instructie bijgewerkt via UI", flow=flow, chars=len(instruction))
    return {"flow": flow, "instruction": instruction}


@router.get("", response_model=list[AssistantResponse])
def get_all(
    skip: int = 0,
    limit: int = 100,
    service: AssistantService = Depends(get_service),
):
    log.infox(
        "Assistants ophalen gestart",
        skip=skip,
        limit=limit,
    )
    result = service.get_all(skip=skip, limit=limit)
    log.infox(
        "Assistants ophalen afgerond",
        skip=skip,
        limit=limit,
        count=len(result) if result is not None else None,
    )
    return result


@router.get("/full", response_model=list[AssistantWithRelations])
def get_all_with_relations(
    skip: int = 0,
    limit: int = 100,
    service: AssistantService = Depends(get_service),
):
    log.infox(
        "Assistants met relaties ophalen gestart",
        skip=skip,
        limit=limit,
    )
    result = service.get_all_with_relations(skip=skip, limit=limit)
    log.infox(
        "Assistants met relaties ophalen afgerond",
        skip=skip,
        limit=limit,
        count=len(result) if result is not None else None,
    )
    return result


@router.get("/{assistant_id}", response_model=AssistantResponse)
def get_by_id(
    assistant_id: int,
    service: AssistantService = Depends(get_service),
):
    log.infox(
        "Assistant ophalen op ID gestart",
        assistant_id=assistant_id,
    )
    result = service.get_by_id(assistant_id)
    log.infox(
        "Assistant ophalen op ID afgerond",
        assistant_id=assistant_id,
        found=result is not None,
    )
    return result


@router.get("/{assistant_id}/full", response_model=AssistantWithRelations)
def get_with_relations(
    assistant_id: int,
    service: AssistantService = Depends(get_service),
):
    log.infox(
        "Assistant met relaties ophalen gestart",
        assistant_id=assistant_id,
    )
    result = service.get_with_relations(assistant_id)
    log.infox(
        "Assistant met relaties ophalen afgerond",
        assistant_id=assistant_id,
        found=result is not None,
    )
    return result


@router.post("", response_model=AssistantResponse, status_code=201)
def create(
    data: AssistantCreate,
    service: AssistantService = Depends(get_service),
    user=Depends(require_user),
):
    log.infox(
        "Assistant aanmaken gestart",
        name=getattr(data, "name", None),
        slug=getattr(data, "slug", None),
        is_active=getattr(data, "is_active", None),
    )
    result = service.create(data, user=user)
    log.infox(
        "Assistant aanmaken afgerond",
        assistant_id=getattr(result, "id", None),
        name=getattr(result, "name", None),
        slug=getattr(result, "slug", None),
    )
    return result


@router.put("/{assistant_id}", response_model=AssistantResponse)
def update(
    assistant_id: int,
    data: AssistantUpdate,
    service: AssistantService = Depends(get_service),
    user=Depends(require_user),
):
    log.infox(
        "Assistant bijwerken gestart",
        assistant_id=assistant_id,
        name=getattr(data, "name", None),
        slug=getattr(data, "slug", None),
        is_active=getattr(data, "is_active", None),
    )
    result = service.update(assistant_id, data, user=user)
    log.infox(
        "Assistant bijwerken afgerond",
        assistant_id=assistant_id,
        result_id=getattr(result, "id", None),
        name=getattr(result, "name", None),
        slug=getattr(result, "slug", None),
    )
    return result


@router.delete("/{assistant_id}")
def delete(
    assistant_id: int,
    service: AssistantService = Depends(get_service),
    user=Depends(require_user),
):
    log.infox(
        "Assistant verwijderen gestart",
        assistant_id=assistant_id,
    )
    result = service.delete(assistant_id, user=user)
    log.infox(
        "Assistant verwijderen afgerond",
        assistant_id=assistant_id,
    )
    return result


# --- assistant <-> tool relation endpoints ---

@router.get(
    "/{assistant_id}/tools",
    response_model=list[AssistantToolMiniResponse],
)
def get_tools_for_assistant(
    assistant_id: int,
    service: AssistantToolService = Depends(get_assistant_tool_service),
):
    log.infox(
        "Tools voor assistant ophalen gestart",
        assistant_id=assistant_id,
    )
    result = service.get_tools_for_assistant(assistant_id)
    log.infox(
        "Tools voor assistant ophalen afgerond",
        assistant_id=assistant_id,
        count=len(result) if result is not None else None,
    )
    return result


@router.post(
    "/{assistant_id}/tools/{tool_id}",
    response_model=AssistantWithRelations,
    status_code=status.HTTP_200_OK,
)
def attach_tool_to_assistant(
    assistant_id: int,
    tool_id: int,
    service: AssistantToolService = Depends(get_assistant_tool_service),
    assistant_service: AssistantService = Depends(get_service),
    user=Depends(require_user),
):
    a = assistant_service.get_by_id(assistant_id)
    if (getattr(a, "assistant_type", "") or "").lower() in {"router","final_answer","answer"} or (getattr(a, "name", "") or "") in {"RouterAssistant","AnswerAssistant"}:
        assert_expert_role(user)
    log.infox(
        "Tool koppelen aan assistant gestart",
        assistant_id=assistant_id,
        tool_id=tool_id,
    )
    result = service.add_tool_to_assistant(assistant_id, tool_id)
    log.infox(
        "Tool koppelen aan assistant afgerond",
        assistant_id=assistant_id,
        tool_id=tool_id,
    )
    return result


@router.delete(
    "/{assistant_id}/tools/{tool_id}",
    response_model=AssistantWithRelations,
    status_code=status.HTTP_200_OK,
)
def detach_tool_from_assistant(
    assistant_id: int,
    tool_id: int,
    service: AssistantToolService = Depends(get_assistant_tool_service),
    assistant_service: AssistantService = Depends(get_service),
    user=Depends(require_user),
):
    a = assistant_service.get_by_id(assistant_id)
    if (getattr(a, "assistant_type", "") or "").lower() in {"router","final_answer","answer"} or (getattr(a, "name", "") or "") in {"RouterAssistant","AnswerAssistant"}:
        assert_expert_role(user)
    log.infox(
        "Tool loskoppelen van assistant gestart",
        assistant_id=assistant_id,
        tool_id=tool_id,
    )
    result = service.remove_tool_from_assistant(assistant_id, tool_id)
    log.infox(
        "Tool loskoppelen van assistant afgerond",
        assistant_id=assistant_id,
        tool_id=tool_id,
    )
    return result

@router.get(
    "/{assistant_id}/skills",
    response_model=list[AssistantSkillMiniResponse],
)
def get_skills_for_assistant(
    assistant_id: int,
    service: SkillService = Depends(get_skill_service),
):
    log.infox(
        "Skills voor assistant ophalen gestart",
        assistant_id=assistant_id,
    )
    result = service.get_skills_for_assistant(assistant_id)
    log.infox(
        "Skills voor assistant ophalen afgerond",
        assistant_id=assistant_id,
        count=len(result) if result is not None else None,
    )
    return result