from __future__ import annotations

from sqlalchemy.orm import Session

from models.skill import Skill
from models.skill_tool import SkillTool
from models.assistant_skill import AssistantSkill


class SkillRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, *, skip: int = 0, limit: int = 100, include_disabled: bool = True):
        q = self.db.query(Skill)

        if not include_disabled:
            q = q.filter(Skill.is_enabled == True)

        return (
            q.order_by(Skill.is_system.desc(), Skill.priority.asc(), Skill.name.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_by_id(self, skill_id: int):
        return self.db.query(Skill).filter(Skill.id == skill_id).first()

    def get_by_name(self, name: str):
        return self.db.query(Skill).filter(Skill.name == name).first()

    def get_system_skills(self):
        return (
            self.db.query(Skill)
            .filter(Skill.is_enabled == True, Skill.is_system == True)
            .order_by(Skill.priority.asc(), Skill.name.asc())
            .all()
        )


    def get_runtime_skills(self):
        return (
            self.db.query(Skill)
            .filter(Skill.is_enabled == True, Skill.is_runtime == True)
            .order_by(Skill.priority.asc(), Skill.name.asc())
            .all()
        )

    @staticmethod
    def _normalize_tags(value):
        """Trim, drop blanks, and de-duplicate (case-insensitive, first-seen casing)."""
        if value is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = str(raw).strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)
        return out

    def create(self, data):
        payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
        if "routing_tags" in payload:
            payload["routing_tags"] = self._normalize_tags(payload["routing_tags"])
        item = Skill(**payload)

        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, skill_id: int, data):
        item = self.get_by_id(skill_id)
        if not item:
            return None

        payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
        if "routing_tags" in payload:
            payload["routing_tags"] = self._normalize_tags(payload["routing_tags"])

        for key, value in payload.items():
            setattr(item, key, value)

        self.db.commit()
        self.db.refresh(item)
        return item

    def delete(self, skill_id: int) -> bool:
        item = self.get_by_id(skill_id)
        if not item:
            return False

        # Cascade-remove link rows explicitly in the same transaction so a deleted
        # skill never leaves a dangling skill_tool / assistant_skill row (SQLite
        # does not enforce the FK `ondelete=CASCADE` unless PRAGMA foreign_keys=ON).
        self.db.query(SkillTool).filter(SkillTool.skill_id == skill_id).delete(
            synchronize_session=False
        )
        self.db.query(AssistantSkill).filter(
            AssistantSkill.skill_id == skill_id
        ).delete(synchronize_session=False)

        self.db.delete(item)
        self.db.commit()
        return True