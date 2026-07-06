from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, or_, select, func
from sqlalchemy.orm import Session

from models.log_entry import LogEntry


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"_unserializable": str(value)}, ensure_ascii=False)


class LogRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        level: str,
        logger: str,
        message: str,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        sequence: Optional[str] = None,
        step: Optional[str] = None,
        duration_ms: Optional[int] = None,
        since_prev_ms: Optional[int] = None,
        context: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
        exc_type: Optional[str] = None,
        exc_text: Optional[str] = None,
    ) -> LogEntry:
        row = LogEntry(
            created_at=datetime.now(timezone.utc),
            level=str(level),
            logger=str(logger),
            message=str(message),
            trace_id=trace_id,
            span_id=span_id,
            sequence=sequence,
            step=step,
            duration_ms=duration_ms,
            since_prev_ms=since_prev_ms,
            context=context,
            extra_json=_safe_json_dumps(extra_fields or {}),
            exc_type=exc_type,
            exc_text=exc_text,
        )

        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)

        return row

    def search(
        self,
        *,
        q: Optional[str] = None,
        level: Optional[str] = None,
        logger: Optional[str] = None,
        trace_id: Optional[str] = None,
        sequence: Optional[str] = None,
        step: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[int, List[LogEntry]]:
        skip = max(0, int(skip))
        limit = max(1, min(int(limit), 500))

        filters = []

        if level:
            filters.append(LogEntry.level == str(level).upper())

        if logger:
            filters.append(LogEntry.logger.ilike(f"%{logger}%"))

        if trace_id:
            filters.append(LogEntry.trace_id == str(trace_id))

        if sequence:
            filters.append(LogEntry.sequence.ilike(f"%{sequence}%"))

        if step:
            filters.append(LogEntry.step.ilike(f"%{step}%"))

        if created_from:
            filters.append(LogEntry.created_at >= created_from)

        if created_to:
            filters.append(LogEntry.created_at <= created_to)

        if q:
            like = f"%{q}%"
            filters.append(
                or_(
                    LogEntry.message.ilike(like),
                    LogEntry.logger.ilike(like),
                    LogEntry.context.ilike(like),
                    LogEntry.extra_json.ilike(like),
                    LogEntry.exc_text.ilike(like),
                )
            )

        stmt = select(LogEntry)

        if filters:
            stmt = stmt.where(and_(*filters))

        total = self.db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()

        rows = (
            self.db.execute(
                stmt.order_by(desc(LogEntry.created_at), desc(LogEntry.id))
                .offset(skip)
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return int(total), rows