# repository/assistant_project_repository.py

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import func, or_, select

from db.database import SessionLocal
from models.assistant_thread import AssistantProjectModel
from services.system_cognition.models import utc_now_iso


class AssistantProjectRepository:
    async def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        domain: Optional[str] = None,
        repository_url: Optional[str] = None,
        local_path: Optional[str] = None,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                row = AssistantProjectModel(
                    id=str(uuid4()),
                    name=name.strip(),
                    description=description,
                    domain=domain,
                    status="active",
                    is_archived=False,
                    repository_url=repository_url,
                    local_path=local_path,
                    metadata_=metadata_ or {},
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                return self._to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def get(self, project_id: str) -> Optional[Dict[str, Any]]:
        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantProjectModel, project_id)
                return self._to_dict(row) if row else None
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def list(
        self,
        *,
        q: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        def _run():
            db = SessionLocal()
            try:
                stmt = select(AssistantProjectModel)
                count_stmt = select(func.count()).select_from(AssistantProjectModel)

                filters = []

                if not include_archived:
                    filters.append(AssistantProjectModel.is_archived.is_(False))

                if q:
                    like = f"%{q.strip()}%"
                    filters.append(
                        or_(
                            AssistantProjectModel.name.ilike(like),
                            AssistantProjectModel.description.ilike(like),
                            AssistantProjectModel.domain.ilike(like),
                        )
                    )

                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

                total = db.execute(count_stmt).scalar_one()

                rows = (
                    db.execute(
                        stmt.order_by(AssistantProjectModel.updated_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                return {
                    "items": [self._to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def update(
        self,
        *,
        project_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = None,
        is_archived: Optional[bool] = None,
        repository_url: Optional[str] = None,
        local_path: Optional[str] = None,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantProjectModel, project_id)
                if not row:
                    return None

                if name is not None:
                    row.name = name.strip()
                if description is not None:
                    row.description = description
                if domain is not None:
                    row.domain = domain
                if status is not None:
                    row.status = status
                if is_archived is not None:
                    row.is_archived = bool(is_archived)
                if repository_url is not None:
                    row.repository_url = repository_url
                if local_path is not None:
                    row.local_path = local_path
                if metadata_ is not None:
                    current = row.metadata_ or {}
                    current.update(metadata_)
                    row.metadata_ = current

                row.updated_at = now

                db.commit()
                db.refresh(row)
                return self._to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def delete_project(
        self, *, project_id: str, delete_threads: bool = True, delete_memories: bool = False
    ) -> Optional[Dict[str, int]]:
        """Delete a project. By default also deletes its threads (+ messages);
        with delete_memories also removes thread- and project-scoped cognition.
        Returns counts, or None if the project does not exist."""
        from models.assistant_thread import AssistantThreadModel, AssistantThreadMessageModel
        from repository.assistant_thread_repository import (
            _delete_thread_cognition, _delete_project_cognition,
        )

        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantProjectModel, project_id)
                if not row:
                    return None
                counts = {"threads": 0, "messages": 0, "memories": 0, "beliefs": 0, "curiosity_jobs": 0}
                thread_ids = [
                    t.id for t in db.query(AssistantThreadModel)
                    .filter(AssistantThreadModel.project_id == project_id).all()
                ]
                if delete_threads:
                    for tid in thread_ids:
                        counts["messages"] += (
                            db.query(AssistantThreadMessageModel)
                            .filter(AssistantThreadMessageModel.thread_id == tid)
                            .delete(synchronize_session=False)
                        )
                        if delete_memories:
                            for k, v in _delete_thread_cognition(db, tid).items():
                                counts[k] += v
                        db.query(AssistantThreadModel).filter(
                            AssistantThreadModel.id == tid
                        ).delete(synchronize_session=False)
                        counts["threads"] += 1
                if delete_memories:
                    for k, v in _delete_project_cognition(db, project_id).items():
                        counts[k] += v
                db.delete(row)
                db.commit()
                return counts
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    def _to_dict(self, row: AssistantProjectModel) -> Dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "domain": row.domain,
            "status": row.status,
            "is_archived": bool(row.is_archived),
            "repository_url": row.repository_url,
            "local_path": row.local_path,
            "metadata_": row.metadata_ or {},
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }