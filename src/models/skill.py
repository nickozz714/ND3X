from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.database import Base


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=False, default="")
    instructions = Column(Text, nullable=False, default="")

    input_schema = Column(JSON, nullable=True)
    output_schema = Column(JSON, nullable=True)

    is_system = Column(Boolean, nullable=False, default=False)
    is_runtime = Column(Boolean, nullable=False, default=False)
    is_enabled = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=100)

    source = Column(String(50), nullable=False, default="local")
    source_name = Column(String(255), nullable=True)
    version = Column(String(50), nullable=False, default="1.0.0")

    # Free-text organisational tags for the Skills overview (filtering). Does NOT
    # affect agent skill-selection (that runs on `description`). Reuses the existing
    # router-era `routing_tags` name already present in the schema/FE type.
    routing_tags = Column(JSON, nullable=True)  # list[str]

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tools = relationship(
        "Tool",
        secondary="skill_tool",
        primaryjoin="Skill.id == SkillTool.skill_id",
        secondaryjoin="Tool.id == SkillTool.tool_id",
        viewonly=True,
    )

    assistants = relationship(
        "Assistant",
        secondary="assistant_skill",
        primaryjoin="Skill.id == AssistantSkill.skill_id",
        secondaryjoin="Assistant.id == AssistantSkill.assistant_id",
        viewonly=True,
    )

    files = relationship(
        "SkillFile",
        back_populates="skill",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SkillFile.relative_path",
    )
