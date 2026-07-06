# services/audit_service.py
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, func, desc, and_

from component.logging import get_logger
from db.database import SessionLocal
from models.audit import AuditTraceEvent


log = get_logger(__name__)


def _safe_json_dumps(obj: Any) -> str:
    log.debugx(
        "Audit JSON serialiseren gestart",
        object_type=type(obj).__name__,
    )
    try:
        result = json.dumps(obj, ensure_ascii=False, default=str)
        log.debugx(
            "Audit JSON serialiseren afgerond",
            object_type=type(obj).__name__,
            result_length=len(result or ""),
        )
        return result
    except Exception:
        log.warningx(
            "Audit JSON serialiseren mislukt, fallback gebruiken",
            object_type=type(obj).__name__,
        )
        result = json.dumps({"_unserializable": str(obj)}, ensure_ascii=False)
        log.debugx(
            "Audit JSON fallback serialiseren afgerond",
            result_length=len(result or ""),
        )
        return result


class AuditService:
    """
    Append-only audit storage for "trace events".
    - One row per event
    - Per thread_id, per turn_id, with seq ordering
    """

    def append_event(
        self,
        *,
        thread_id: str,
        turn_id: int,
        seq: int,
        type: str,
        summary: str,
        data: Dict[str, Any],
        level: str = "info",
        ts: Optional[float] = None,
    ) -> int:
        log.infox(
            "Audit event opslaan gestart",
            thread_id=thread_id,
            turn_id=turn_id,
            seq=seq,
            type=type,
            level=level,
            summary=summary,
            has_ts=ts is not None,
            data_keys=list((data or {}).keys()) if isinstance(data or {}, dict) else None,
        )

        ts_val = float(ts if ts is not None else time.time())

        row = AuditTraceEvent(
            ts=ts_val,
            thread_id=str(thread_id),
            turn_id=int(turn_id),
            seq=int(seq),
            type=str(type),
            level=str(level),
            summary=str(summary),
            data_json=_safe_json_dumps(data or {}),
        )

        log.debugx(
            "AuditTraceEvent row opgebouwd",
            thread_id=str(thread_id),
            turn_id=int(turn_id),
            seq=int(seq),
            type=str(type),
            level=str(level),
            ts=ts_val,
            data_json_length=len(row.data_json or ""),
        )

        with SessionLocal() as db:
            log.debugx(
                "Audit database sessie geopend voor append_event",
                thread_id=str(thread_id),
                turn_id=int(turn_id),
                seq=int(seq),
            )
            db.add(row)
            log.debugx(
                "Audit row toegevoegd aan sessie",
                thread_id=str(thread_id),
                turn_id=int(turn_id),
                seq=int(seq),
            )
            db.commit()
            log.debugx(
                "Audit database commit uitgevoerd",
                thread_id=str(thread_id),
                turn_id=int(turn_id),
                seq=int(seq),
            )
            db.refresh(row)
            log.infox(
                "Audit event opslaan afgerond",
                audit_event_id=int(row.id),
                thread_id=str(thread_id),
                turn_id=int(turn_id),
                seq=int(seq),
                type=str(type),
                level=str(level),
            )
            return int(row.id)

    def get_thread_events(
        self,
        *,
        thread_id: str,
        limit: int = 500,
        offset: int = 0,
        newest_first: bool = True,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        log.infox(
            "Audit thread events ophalen gestart",
            thread_id=thread_id,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )

        limit = max(1, min(int(limit), 2000))
        offset = max(0, int(offset))

        log.debugx(
            "Audit thread events pagination genormaliseerd",
            thread_id=thread_id,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )

        with SessionLocal() as db:
            log.debugx(
                "Audit database sessie geopend voor get_thread_events",
                thread_id=str(thread_id),
            )
            base = select(AuditTraceEvent).where(AuditTraceEvent.thread_id == str(thread_id))
            total = db.execute(
                select(func.count()).select_from(base.subquery())
            ).scalar_one()

            log.debugx(
                "Audit thread events totaal berekend",
                thread_id=str(thread_id),
                total=int(total),
            )

            order = desc(AuditTraceEvent.ts) if newest_first else AuditTraceEvent.ts
            rows = (
                db.execute(base.order_by(order, desc(AuditTraceEvent.id)).limit(limit).offset(offset))
                .scalars()
                .all()
            )

            log.debugx(
                "Audit thread events rows opgehaald",
                thread_id=str(thread_id),
                total=int(total),
                row_count=len(rows or []),
                limit=limit,
                offset=offset,
            )

        result = [r.to_dict() for r in rows]

        log.infox(
            "Audit thread events ophalen afgerond",
            thread_id=str(thread_id),
            total=int(total),
            result_count=len(result),
            limit=limit,
            offset=offset,
            newest_first=newest_first,
        )
        return int(total), result

    def search(
        self,
        *,
        q: Optional[str] = None,
        thread_id: Optional[str] = None,
        type: Optional[str] = None,
        level: Optional[str] = None,
        ts_from: Optional[float] = None,
        ts_to: Optional[float] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Simple search using LIKE on summary and data_json.
        (We can upgrade to FTS5 later for speed/quality.)
        """
        log.infox(
            "Audit search gestart",
            q=q,
            thread_id=thread_id,
            type=type,
            level=level,
            ts_from=ts_from,
            ts_to=ts_to,
            limit=limit,
            offset=offset,
        )

        limit = max(1, min(int(limit), 2000))
        offset = max(0, int(offset))

        log.debugx(
            "Audit search pagination genormaliseerd",
            limit=limit,
            offset=offset,
        )

        filters = []
        if thread_id:
            filters.append(AuditTraceEvent.thread_id == str(thread_id))
        if type:
            filters.append(AuditTraceEvent.type == str(type))
        if level:
            filters.append(AuditTraceEvent.level == str(level))
        if ts_from is not None:
            filters.append(AuditTraceEvent.ts >= float(ts_from))
        if ts_to is not None:
            filters.append(AuditTraceEvent.ts <= float(ts_to))

        log.debugx(
            "Audit search filters opgebouwd",
            filter_count=len(filters),
            has_q=bool(q),
            thread_id=thread_id,
            type=type,
            level=level,
            has_ts_from=ts_from is not None,
            has_ts_to=ts_to is not None,
        )

        with SessionLocal() as db:
            log.debugx(
                "Audit database sessie geopend voor search",
                filter_count=len(filters),
                has_q=bool(q),
            )
            stmt = select(AuditTraceEvent)
            if filters:
                stmt = stmt.where(and_(*filters))
                log.debugx(
                    "Audit search filters toegepast",
                    filter_count=len(filters),
                )

            if q:
                like = f"%{q}%"
                stmt = stmt.where(
                    AuditTraceEvent.summary.like(like) | AuditTraceEvent.data_json.like(like)
                )
                log.debugx(
                    "Audit search tekstfilter toegepast",
                    q=q,
                    like=like,
                )

            total = db.execute(
                select(func.count()).select_from(stmt.subquery())
            ).scalar_one()

            log.debugx(
                "Audit search totaal berekend",
                total=int(total),
                limit=limit,
                offset=offset,
            )

            rows = (
                db.execute(
                    stmt.order_by(desc(AuditTraceEvent.ts), desc(AuditTraceEvent.id))
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )

            log.debugx(
                "Audit search rows opgehaald",
                total=int(total),
                row_count=len(rows or []),
                limit=limit,
                offset=offset,
            )

        result = [r.to_dict() for r in rows]

        log.infox(
            "Audit search afgerond",
            total=int(total),
            result_count=len(result),
            q=q,
            thread_id=thread_id,
            type=type,
            level=level,
            limit=limit,
            offset=offset,
        )
        return int(total), result