from sqlalchemy import Column, Integer, String, Text, JSON, DateTime, Boolean, Float
from sqlalchemy.orm import relationship
from db.database import Base
from models.assistant_tool import assistant_tool


class Assistant(Base):
    __tablename__ = "assistant"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=False)

    # Editable assistant content
    instruction = Column(Text, nullable=False)
    schema = Column(JSON, nullable=False)

    # Runtime metadata
    assistant_type = Column(String, nullable=False, default="planner")  # router | planner | final_answer
    routing_tags = Column(JSON, nullable=True)  # e.g. ["todos", "notes", "code"]
    model = Column(String, nullable=True)
    temperature = Column(Float, nullable=True)
    priority = Column(Integer, nullable=True, default=100)
    is_router_selectable = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False)

    tools = relationship(
        "Tool",
        secondary=assistant_tool,
        back_populates="assistants",
    )

    skills = relationship(
        "Skill",
        secondary="assistant_skill",
        primaryjoin="Assistant.id == AssistantSkill.assistant_id",
        secondaryjoin="Skill.id == AssistantSkill.skill_id",
        viewonly=True,
    )