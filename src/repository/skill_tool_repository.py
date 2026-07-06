from __future__ import annotations

from sqlalchemy.orm import Session

from models.skill_tool import SkillTool
from models.tool import Tool


class SkillToolRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_for_skill(self, skill_id: int, *, enabled_only: bool = True):
        q = (
            self.db.query(SkillTool, Tool)
            .join(Tool, Tool.id == SkillTool.tool_id)
            .filter(SkillTool.skill_id == skill_id)
        )

        if enabled_only:
            q = q.filter(
                SkillTool.is_enabled == True,
                Tool.is_enabled == True,
            )

        return q.order_by(Tool.name.asc()).all()

    def link(self, *, skill_id: int, tool_id: int):
        existing = (
            self.db.query(SkillTool)
            .filter(
                SkillTool.skill_id == skill_id,
                SkillTool.tool_id == tool_id,
            )
            .first()
        )

        if existing:
            existing.is_enabled = True
            self.db.commit()
            self.db.refresh(existing)
            return existing

        row = SkillTool(
            skill_id=skill_id,
            tool_id=tool_id,
            is_enabled=True,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def unlink(self, *, skill_id: int, tool_id: int) -> bool:
        row = (
            self.db.query(SkillTool)
            .filter(
                SkillTool.skill_id == skill_id,
                SkillTool.tool_id == tool_id,
            )
            .first()
        )

        if not row:
            return False

        self.db.delete(row)
        self.db.commit()
        return True