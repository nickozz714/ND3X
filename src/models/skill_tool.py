from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Integer, UniqueConstraint

from db.database import Base


class SkillTool(Base):
    __tablename__ = "skill_tool"

    id = Column(Integer, primary_key=True, index=True)

    skill_id = Column(
        Integer,
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tool_id = Column(
        Integer,
        ForeignKey("tool.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    is_enabled = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("skill_id", "tool_id", name="uq_skill_tool"),
    )