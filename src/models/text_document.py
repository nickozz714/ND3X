from datetime import datetime
from typing import List

from sqlalchemy import String, Integer, DateTime, Index, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base



class TextDocument(Base):
    __tablename__ = "text_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[List["TextChunk"]] = relationship(
        back_populates="doc",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

class TextChunk(Base):
    __tablename__ = "text_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("text_docs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    doc: Mapped["TextDocument"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("idx_text_chunks_embedding_id", "embedding_id"),
    )

class EmbeddingSeq(Base):
    __tablename__ = "embedding_seq"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    next_id: Mapped[int] = mapped_column(Integer, nullable=False)