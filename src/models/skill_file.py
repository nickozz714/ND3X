from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.database import Base


class SkillFile(Base):
    __tablename__ = "skill_files"
    __table_args__ = (
        UniqueConstraint("skill_id", "relative_path", name="uq_skill_files_skill_path"),
    )

    id = Column(Integer, primary_key=True, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path = Column(String(512), nullable=False)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(255), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    checksum_sha256 = Column(String(64), nullable=False, default="")
    is_editable = Column(Boolean, nullable=False, default=True)
    is_executable = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    skill = relationship("Skill", back_populates="files")
