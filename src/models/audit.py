# src/audit/models.py
from __future__ import annotations

import json
from typing import Any, Dict

from sqlalchemy import Column, Float, Index, Integer, String, Text

from db.database import Base


class AuditTraceEvent(Base):
    __tablename__ = "audit_trace_events"

    id = Column(Integer, primary_key=True, autoincrement=True)

    ts = Column(Float, nullable=False)  # epoch seconds
    thread_id = Column(String(255), nullable=False, index=True)
    turn_id = Column(Integer, nullable=False)
    seq = Column(Integer, nullable=False)

    type = Column(String(64), nullable=False, index=True)     # plan, tool_call, ...
    level = Column(String(16), nullable=False)                # info|warn|error
    summary = Column(String(255), nullable=False)

    data_json = Column(Text, nullable=False)

    __table_args__ = (
        Index("idx_audit_thread_ts", "thread_id", "ts"),
        Index("idx_audit_thread_turn_seq", "thread_id", "turn_id", "seq"),
        Index("idx_audit_type_ts", "type", "ts"),
        Index("idx_audit_ts", "ts"),
    )

    def to_dict(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.data_json or "{}")
        except Exception:
            data = {"_raw": self.data_json}

        return {
            "id": self.id,
            "ts": self.ts,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "seq": self.seq,
            "type": self.type,
            "level": self.level,
            "summary": self.summary,
            "data": data,
        }
