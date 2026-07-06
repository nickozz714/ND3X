from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from authentication.dependencies import require_user, require_admin_user
from services.authz_service import assert_expert_role, user_has_role
from sqlalchemy.orm import Session

from db.database import get_db
from schemas.skill import (
    AssistantSkillLinkRequest,
    SkillCreate,
    SkillMarkdownImport,
    SkillRead,
    SkillToolLinkRequest,
    SkillUpdate, SkillWithRelations, SkillToolMiniResponse, SkillAssistantMiniResponse,
    SkillFilePayload, SkillFileUpdatePayload, SkillFileRead, SkillFileDetail,
)
from services.assistants.skill_service import SkillService
from services.assistants.skill_file_service import SkillFileService


router = APIRouter(
    prefix="/admin/skills",
    tags=["Skills"],
)


@router.get("", response_model=list[SkillRead])
def list_skills(
    skip: int = 0,
    limit: int = 100,
    include_disabled: bool = Query(True),
    db: Session = Depends(get_db),
):
    return SkillService(db).get_all(
        skip=skip,
        limit=limit,
        include_disabled=include_disabled,
    )


@router.post("/generate")
async def generate_skill_with_ai(body: dict, db: Session = Depends(get_db), user=Depends(require_user)):
    """Generate a draft skill from wizard answers using the AI model on the
    cognition/planner slot. Returns a draft to review + save."""
    from services.assistants.skill_ai import generate_skill
    try:
        return await generate_skill(db, body or {})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



def _assert_skill_file_list_allowed(skill, user) -> None:
    if bool(getattr(skill, "is_system", False) or getattr(skill, "is_runtime", False)) and not user_has_role(user, "Expert"):
        raise HTTPException(status_code=403, detail="Expert role required for protected skill files")


@router.get("/{skill_id}/files", response_model=list[SkillFileRead])
def list_skill_files(
    skill_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    skill = SkillService(db).get_by_id(skill_id)
    _assert_skill_file_list_allowed(skill, user)
    return SkillFileService(db).list_skill_files(skill_id)


@router.get("/{skill_id}/files/{file_id}", response_model=SkillFileDetail)
def get_skill_file(
    skill_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    return SkillFileService(db).get_skill_file(skill_id, file_id, include_content=True)


@router.post("/{skill_id}/files", response_model=SkillFileRead)
def create_skill_file(
    skill_id: int,
    data: SkillFilePayload,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    item = SkillFileService(db).create_or_update_skill_file(
        skill_id,
        data.relative_path,
        data.content,
        data.model_dump(exclude={"relative_path", "content"}),
    )
    return SkillFileService(db).to_metadata(item, include_content=False)


@router.put("/{skill_id}/files/{file_id}", response_model=SkillFileRead)
def update_skill_file(
    skill_id: int,
    file_id: int,
    data: SkillFileUpdatePayload,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    item = SkillFileService(db).update_skill_file(
        skill_id,
        file_id,
        content=data.content,
        metadata=data.model_dump(exclude_unset=True, exclude={"content"}),
    )
    return SkillFileService(db).to_metadata(item, include_content=False)


@router.delete("/{skill_id}/files/{file_id}")
def delete_skill_file(
    skill_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    return SkillFileService(db).delete_skill_file(skill_id, file_id)

@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(
    skill_id: int,
    db: Session = Depends(get_db),
):
    return SkillService(db).get_by_id(skill_id)

@router.get("/full", response_model=list[SkillWithRelations])
def list_skills_with_relations(
    skip: int = 0,
    limit: int = 100,
    include_disabled: bool = Query(True),
    db: Session = Depends(get_db),
):
    return SkillService(db).get_all_with_relations(
        skip=skip,
        limit=limit,
        include_disabled=include_disabled,
    )


@router.get("/{skill_id}/full", response_model=SkillWithRelations)
def get_skill_with_relations(
    skill_id: int,
    db: Session = Depends(get_db),
):
    return SkillService(db).get_with_relations(skill_id)


@router.get("/{skill_id}/tools", response_model=list[SkillToolMiniResponse])
def get_tools_for_skill(
    skill_id: int,
    db: Session = Depends(get_db),
):
    return SkillService(db).get_tools_for_skill(skill_id)


@router.get("/{skill_id}/assistants", response_model=list[SkillAssistantMiniResponse])
def get_assistants_for_skill(
    skill_id: int,
    db: Session = Depends(get_db),
):
    return SkillService(db).get_assistants_for_skill(skill_id)

@router.post("", response_model=SkillRead)
def create_skill(
    data: SkillCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).create(data, user=user)


@router.post("/import-markdown", response_model=SkillRead)
def import_skill_markdown(
    data: SkillMarkdownImport,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).import_markdown(data, user=user)


@router.put("/{skill_id}", response_model=SkillRead)
def update_skill(
    skill_id: int,
    data: SkillUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).update(skill_id, data, user=user)


class _SkillEnabledIn(BaseModel):
    enabled: bool


@router.post("/{skill_id}/enabled", response_model=SkillRead)
def set_skill_enabled(
    skill_id: int,
    body: _SkillEnabledIn,
    db: Session = Depends(get_db),
    user=Depends(require_admin_user),
):
    """Admin: turn a (system/runtime/normal) skill on/off. Disabling a system skill
    or contract changes what the assistant can do — handle with care."""
    return SkillService(db).update(skill_id, SkillUpdate(is_enabled=body.enabled), user=user)


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).delete(skill_id, user=user)


class _BulkDeleteIn(BaseModel):
    ids: list[int]


@router.post("/bulk-delete")
def bulk_delete_skills(
    body: _BulkDeleteIn,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Delete several skills at once. Best-effort per id: protected/missing skills
    are reported as failed without aborting the rest."""
    return SkillService(db).bulk_delete(body.ids, user=user)


@router.post("/{skill_id}/tools")
def link_tool_to_skill(
    skill_id: int,
    data: SkillToolLinkRequest,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).link_tool_to_skill(
        skill_id=skill_id,
        tool_id=data.tool_id,
        user=user,
    )


@router.delete("/{skill_id}/tools/{tool_id}")
def unlink_tool_from_skill(
    skill_id: int,
    tool_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).unlink_tool_from_skill(
        skill_id=skill_id,
        tool_id=tool_id,
        user=user,
    )


@router.post("/assistants/{assistant_id}")
def link_skill_to_assistant(
    assistant_id: int,
    data: AssistantSkillLinkRequest,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).link_skill_to_assistant(
        assistant_id=assistant_id,
        skill_id=data.skill_id,
        user=user,
    )


@router.delete("/assistants/{assistant_id}/{skill_id}")
def unlink_skill_from_assistant(
    assistant_id: int,
    skill_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return SkillService(db).unlink_skill_from_assistant(
        assistant_id=assistant_id,
        skill_id=skill_id,
        user=user,
    )