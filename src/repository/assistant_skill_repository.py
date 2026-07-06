from __future__ import annotations

from sqlalchemy.orm import Session

from models.assistant import Assistant
from models.assistant_skill import AssistantSkill
from models.skill import Skill


class AssistantSkillRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_for_assistant(self, assistant_id: int, *, enabled_only: bool = True):
        q = (
            self.db.query(AssistantSkill, Skill)
            .join(Skill, Skill.id == AssistantSkill.skill_id)
            .filter(AssistantSkill.assistant_id == assistant_id)
        )

        if enabled_only:
            q = q.filter(
                AssistantSkill.is_enabled == True,
                Skill.is_enabled == True,
            )

        return q.order_by(Skill.priority.asc(), Skill.name.asc()).all()

    def link(self, *, assistant_id: int, skill_id: int):
        existing = (
            self.db.query(AssistantSkill)
            .filter(
                AssistantSkill.assistant_id == assistant_id,
                AssistantSkill.skill_id == skill_id,
            )
            .first()
        )

        if existing:
            existing.is_enabled = True
            self.db.commit()
            self.db.refresh(existing)
            return existing

        row = AssistantSkill(
            assistant_id=assistant_id,
            skill_id=skill_id,
            is_enabled=True,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def unlink(self, *, assistant_id: int, skill_id: int) -> bool:
        row = (
            self.db.query(AssistantSkill)
            .filter(
                AssistantSkill.assistant_id == assistant_id,
                AssistantSkill.skill_id == skill_id,
            )
            .first()
        )

        if not row:
            return False

        self.db.delete(row)
        self.db.commit()
        return True

    def get_assistants_for_skill(self, skill_id: int, *, enabled_only: bool = True):
        q = (
            self.db.query(AssistantSkill, Assistant)
            .join(Assistant, Assistant.id == AssistantSkill.assistant_id)
            .filter(AssistantSkill.skill_id == skill_id)
        )

        if enabled_only:
            q = q.filter(
                AssistantSkill.is_enabled == True,
                Assistant.is_active == True,
                Assistant.deleted_at.is_(None),
            )

        return q.order_by(Assistant.name.asc()).all()