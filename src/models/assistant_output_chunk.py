from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Index,
    UniqueConstraint,
)
from db.database import Base


class AssistantOutputChunk(Base):
    __tablename__ = "assistant_output_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # stable logical output id shared by all chunks of one output
    output_id = Column(String(64), nullable=False, index=True)

    # useful lookup / audit metadata
    session_id = Column(String(255), nullable=True, index=True)
    turn_id = Column(Integer, nullable=False, index=True)
    assistant_name = Column(String(255), nullable=False, index=True)

    # chunk ordering
    chunk_index = Column(Integer, nullable=False)
    chunk_count = Column(Integer, nullable=False)

    # optional lightweight metadata
    kind = Column(String(64), nullable=False, default="assistant_output")
    content = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("output_id", "chunk_index", name="uq_assistant_output_chunk_idx"),
        Index("ix_assistant_output_chunks_output_order", "output_id", "chunk_index"),
    )