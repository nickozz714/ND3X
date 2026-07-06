from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from component.config import settings
from repository.file_log_repository import FileLogRepository


router = APIRouter(prefix="/logs", tags=["logs"])


def _repo() -> FileLogRepository:
    log_file = (settings.LOG_FILE or "logs/app.log")
    return FileLogRepository(log_file=log_file)


@router.get("")
def list_logs(
    q: Optional[str] = None,
    level: Optional[str] = None,
    logger: Optional[str] = None,
    trace_id: Optional[str] = None,
    sequence: Optional[str] = None,
    step: Optional[str] = None,
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
    skip: int = 0,
    limit: int = 100,
):
    total, items = _repo().search(
        q=q,
        level=level,
        logger=logger,
        trace_id=trace_id,
        sequence=sequence,
        step=step,
        created_from=created_from,
        created_to=created_to,
        skip=skip,
        limit=limit,
    )

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "items": items,
    }