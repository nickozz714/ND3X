from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from db.database import Base


class LogEntry(Base):
    __tablename__ = "log_entries"

    id = Column(Integer, primary_key=True, index=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    level = Column(String(20), nullable=False, index=True)
    logger = Column(String(255), nullable=False, index=True)
    message = Column(Text, nullable=False)

    trace_id = Column(String(64), nullable=True, index=True)
    span_id = Column(String(64), nullable=True, index=True)

    sequence = Column(String(255), nullable=True, index=True)
    step = Column(String(255), nullable=True, index=True)

    duration_ms = Column(Integer, nullable=True)
    since_prev_ms = Column(Integer, nullable=True)

    context = Column(Text, nullable=True)
    extra_json = Column(Text, nullable=True)

    exc_type = Column(String(255), nullable=True, index=True)
    exc_text = Column(Text, nullable=True)


Index("ix_log_entries_created_level", LogEntry.created_at, LogEntry.level)
Index("ix_log_entries_logger_created", LogEntry.logger, LogEntry.created_at)
Index("ix_log_entries_trace_created", LogEntry.trace_id, LogEntry.created_at)