from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from repository.assistant_skill_repository import AssistantSkillRepository
from repository.skill_repository import SkillRepository
from repository.skill_tool_repository import SkillToolRepository
from repository.tool_repository import ToolRepository
from services.assistants.assistant_service import AssistantService
from schemas.skill import SkillCreate, SkillMarkdownImport, SkillUpdate
from services.assistants.skill_file_service import SkillFileService, validate_skill_relative_path




def _is_protected_skill_obj(skill) -> bool:
    return bool(getattr(skill, "is_system", False) or getattr(skill, "is_runtime", False))


def _is_protected_skill_payload(data) -> bool:
    return bool(getattr(data, "is_system", False) or getattr(data, "is_runtime", False))

class SkillService:
    def __init__(self, db: Session):
        self.db = db
        self.skill_repo = SkillRepository(db)
        self.assistant_skill_repo = AssistantSkillRepository(db)
        self.skill_tool_repo = SkillToolRepository(db)
        self.tool_repo = ToolRepository(db)
        self.assistant_service = AssistantService(db)
        self.skill_file_service = SkillFileService(db)

    def get_all(self, skip: int = 0, limit: int = 100, include_disabled: bool = True):
        return self.skill_repo.get_all(
            skip=skip,
            limit=limit,
            include_disabled=include_disabled,
        )

    def get_by_id(self, skill_id: int):
        item = self.skill_repo.get_by_id(skill_id)
        if not item:
            raise HTTPException(status_code=404, detail="Skill not found")
        return item

    def create(self, data: SkillCreate, user=None):
        if _is_protected_skill_payload(data):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        existing = self.skill_repo.get_by_name(data.name)
        if existing:
            raise HTTPException(status_code=409, detail="Skill name already exists")
        return self.skill_repo.create(data)

    def import_markdown(self, data: SkillMarkdownImport, user=None):
        if bool(getattr(data, "is_system", False) or getattr(data, "is_runtime", False)):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        existing = self.skill_repo.get_by_name(data.name)
        if existing:
            raise HTTPException(status_code=409, detail="Skill name already exists")

        for file_payload in data.files:
            validate_skill_relative_path(file_payload.relative_path)

        payload = SkillCreate(
            name=data.name,
            display_name=data.name.replace("_", " ").title(),
            description=data.description,
            instructions=data.markdown,
            is_system=data.is_system,
            is_enabled=False,
            priority=data.priority,
            source="imported_md",
            source_name=data.source_name,
            version="1.0.0",
        )
        skill = self.skill_repo.create(payload)
        for file_payload in data.files:
            self.skill_file_service.create_or_update_skill_file(
                skill.id,
                file_payload.relative_path,
                file_payload.content,
                file_payload.model_dump(exclude={"relative_path", "content"}),
            )
        return skill

    def update(self, skill_id: int, data: SkillUpdate, user=None):
        existing_skill = self.get_by_id(skill_id)
        if _is_protected_skill_obj(existing_skill) or bool(getattr(data, "is_system", False) or getattr(data, "is_runtime", False)):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        if data.name:
            existing = self.skill_repo.get_by_name(data.name)
            if existing and existing.id != skill_id:
                raise HTTPException(status_code=409, detail="Skill name already exists")

        item = self.skill_repo.update(skill_id, data)
        if not item:
            raise HTTPException(status_code=404, detail="Skill not found")
        return item

    def delete(self, skill_id: int, user=None):
        existing_skill = self.get_by_id(skill_id)
        if _is_protected_skill_obj(existing_skill):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        ok = self.skill_repo.delete(skill_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Skill not found")
        return {"success": True}

    def bulk_delete(self, skill_ids: list[int], user=None):
        """Delete several skills, best-effort. Each id is independent: a protected
        skill you may not delete, or a missing id, is reported as failed rather
        than aborting the whole batch."""
        deleted = 0
        results: list[dict] = []
        for sid in skill_ids:
            try:
                self.delete(sid, user=user)
                deleted += 1
                results.append({"id": sid, "status": "deleted"})
            except HTTPException as exc:
                results.append({"id": sid, "status": "failed", "detail": str(exc.detail)})
            except Exception as exc:  # noqa: BLE001 — one bad id must not sink the batch
                results.append({"id": sid, "status": "failed", "detail": str(exc)[:200]})
        return {
            "deleted": deleted,
            "failed": len(skill_ids) - deleted,
            "total": len(skill_ids),
            "results": results,
        }

    def link_skill_to_assistant(self, *, assistant_id: int, skill_id: int, user=None):
        skill_obj = self.get_by_id(skill_id)
        if _is_protected_skill_obj(skill_obj):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        self.assistant_service.get_by_id(assistant_id)
        self.get_by_id(skill_id)

        return self.assistant_skill_repo.link(
            assistant_id=assistant_id,
            skill_id=skill_id,
        )

    def unlink_skill_from_assistant(self, *, assistant_id: int, skill_id: int, user=None):
        skill_obj = self.get_by_id(skill_id)
        if _is_protected_skill_obj(skill_obj):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        ok = self.assistant_skill_repo.unlink(
            assistant_id=assistant_id,
            skill_id=skill_id,
        )

        if not ok:
            raise HTTPException(status_code=404, detail="Assistant skill link not found")

        return {"success": True}

    def link_tool_to_skill(self, *, skill_id: int, tool_id: int, user=None):
        skill_obj = self.get_by_id(skill_id)
        if _is_protected_skill_obj(skill_obj):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        self.get_by_id(skill_id)

        tool = self.tool_repo.get_by_id(tool_id)
        if not tool:
            raise HTTPException(status_code=404, detail="Tool not found")

        return self.skill_tool_repo.link(
            skill_id=skill_id,
            tool_id=tool_id,
        )

    def unlink_tool_from_skill(self, *, skill_id: int, tool_id: int, user=None):
        skill_obj = self.get_by_id(skill_id)
        if _is_protected_skill_obj(skill_obj):
            from services.authz_service import assert_expert_role
            assert_expert_role(user)
        ok = self.skill_tool_repo.unlink(
            skill_id=skill_id,
            tool_id=tool_id,
        )

        if not ok:
            raise HTTPException(status_code=404, detail="Skill tool link not found")

        return {"success": True}

    def get_tools_for_skill(self, skill_id: int):
        self.get_by_id(skill_id)

        rows = self.skill_tool_repo.get_for_skill(
            skill_id,
            enabled_only=False,
        )

        return [tool for _, tool in rows]

    def get_assistants_for_skill(self, skill_id: int):
        self.get_by_id(skill_id)

        rows = self.assistant_skill_repo.get_assistants_for_skill(
            skill_id,
            enabled_only=False,
        )

        return [assistant for _, assistant in rows]

    def get_with_relations(self, skill_id: int):
        skill = self.get_by_id(skill_id)

        # Pydantic can serialize a dict into SkillWithRelations
        return {
            **skill.__dict__,
            "tools": self.get_tools_for_skill(skill_id),
            "assistants": self.get_assistants_for_skill(skill_id),
        }

    def get_all_with_relations(self, skip: int = 0, limit: int = 100, include_disabled: bool = True):
        skills = self.get_all(
            skip=skip,
            limit=limit,
            include_disabled=include_disabled,
        )

        return [
            self.get_with_relations(skill.id)
            for skill in skills
        ]

    def get_skills_for_assistant(self, assistant_id: int):
        self.assistant_service.get_by_id(assistant_id)

        rows = self.assistant_skill_repo.get_for_assistant(
            assistant_id,
            enabled_only=False,
        )

        return [skill for _, skill in rows]