from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Integer, UniqueConstraint

from db.database import Base


class AssistantSkill(Base):
    __tablename__ = "assistant_skill"

    id = Column(Integer, primary_key=True, index=True)

    assistant_id = Column(
        Integer,
        ForeignKey("assistant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    skill_id = Column(
        Integer,
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    is_enabled = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("assistant_id", "skill_id", name="uq_assistant_skill"),
    )