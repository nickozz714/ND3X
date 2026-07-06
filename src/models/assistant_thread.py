# models/assistant_thread.py

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base

class AssistantProjectModel(Base):
    __tablename__ = "assistant_projects"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Useful for routing/memory decisions
    domain: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Optional references to external systems
    repository_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Flexible future metadata
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    threads = relationship(
        "AssistantThreadModel",
        back_populates="project",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_assistant_projects_name", "name"),
        Index("idx_assistant_projects_status", "status"),
        Index("idx_assistant_projects_archived", "is_archived"),
        Index("idx_assistant_projects_domain", "domain"),
    )

class AssistantThreadModel(Base):
    __tablename__ = "assistant_threads"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("assistant_projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_turn_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    project = relationship(
        "AssistantProjectModel",
        back_populates="threads",
    )

    __table_args__ = (
        Index("idx_assistant_threads_project", "project_id"),
        Index("idx_assistant_threads_status", "status"),
        Index("idx_assistant_threads_archived", "is_archived"),
        Index("idx_assistant_threads_updated", "updated_at"),
    )


class AssistantThreadMessageModel(Base):
    __tablename__ = "assistant_thread_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    thread_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("assistant_threads.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Only visible user/assistant messages.
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # User-flagged "important" — forces this message into cognition
    # (memory/belief/curiosity) regardless of the triviality router.
    important: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # The agent's running commentary (narration + tool steps) that produced this
    # assistant message — so the Claude-Code-style step thread survives a reload.
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    thread = relationship("AssistantThreadModel")

    __table_args__ = (
        Index("idx_thread_messages_thread", "thread_id"),
        Index("idx_thread_messages_thread_seq", "thread_id", "sequence"),
        Index("idx_thread_messages_created", "created_at"),
    )