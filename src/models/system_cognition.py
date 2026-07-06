from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text, JSON, Index, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class MemoryModel(Base):
    __tablename__ = "system_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, default="note")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="global")
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("assistant_projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_system_memories_thread", "thread_id"),
        Index("idx_system_memories_project", "project_id"),
        Index("idx_system_memories_type", "type"),
        Index("idx_system_memories_scope", "scope"),
        Index("idx_system_memories_embedding_hash", "embedding_hash"),
    )


class BeliefModel(Base):
    __tablename__ = "system_beliefs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    insights: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    future_use: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(160), nullable=True)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="tentative")
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="global")
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("assistant_projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    use_when: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    contradictions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_verified_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_system_beliefs_topic", "topic"),
        Index("idx_system_beliefs_thread", "thread_id"),
        Index("idx_system_beliefs_scope", "scope"),
        Index("idx_system_beliefs_status", "status"),
        Index("idx_system_beliefs_embedding_hash", "embedding_hash"),
    )


class CuriosityJobModel(Base):
    __tablename__ = "system_curiosity_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    depth: Mapped[str] = mapped_column(String(40), nullable=False, default="small")
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")

    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="thread")
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("assistant_projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_system_curiosity_jobs_status", "status"),
        Index("idx_system_curiosity_jobs_topic", "topic"),
        Index("idx_system_curiosity_jobs_priority", "priority"),
        Index("idx_system_curiosity_jobs_scope", "scope"),
        Index("idx_system_curiosity_jobs_thread", "thread_id"),
        Index("idx_system_curiosity_jobs_project", "project_id"),
    )

class MemoryInjectionModel(Base):
    __tablename__ = "system_memory_injections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Real chat thread id, NOT cognition_<thread_id>.
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # "memory" or "belief"
    memory_kind: Mapped[str] = mapped_column(String(40), nullable=False)

    # system_memories.id or system_beliefs.id
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "memory_kind",
            "memory_id",
            name="uq_system_memory_injection_thread_kind_memory",
        ),
        Index("idx_system_memory_injections_thread", "thread_id"),
        Index("idx_system_memory_injections_memory", "memory_kind", "memory_id"),
    )