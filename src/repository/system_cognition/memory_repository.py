from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from db.database import SessionLocal
from models.system_cognition import MemoryModel
from services.system_cognition.models import MemoryRecord, utc_now_iso


class MemoryRepository:
    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        record.updated_at = utc_now_iso()

        def _run() -> MemoryRecord:
            db = SessionLocal()
            try:
                existing = db.get(MemoryModel, record.id)

                if existing:
                    existing.type = record.type
                    existing.content = record.content
                    existing.scope = record.scope
                    existing.thread_id = record.thread_id
                    existing.project_id = record.project_id
                    existing.importance = float(record.importance)
                    existing.pinned = bool(record.pinned)
                    existing.metadata_ = record.metadata_ or {}
                    existing.embedding = getattr(record, "embedding", None)
                    existing.embedding_model = getattr(record, "embedding_model", None)
                    existing.embedding_hash = getattr(record, "embedding_hash", None)
                    existing.embedding_updated_at = getattr(record, "embedding_updated_at", None)
                    existing.updated_at = record.updated_at
                else:
                    db.add(
                        MemoryModel(
                            id=record.id,
                            type=record.type,
                            content=record.content,
                            scope=record.scope,
                            thread_id=record.thread_id,
                            project_id=record.project_id,
                            importance=float(record.importance),
                            pinned=bool(record.pinned),
                            metadata_=record.metadata_ or {},
                            embedding=getattr(record, "embedding", None),
                            embedding_model=getattr(record, "embedding_model", None),
                            embedding_hash=getattr(record, "embedding_hash", None),
                            embedding_updated_at=getattr(record, "embedding_updated_at", None),
                            created_at=record.created_at,
                            updated_at=record.updated_at,
                        )
                    )

                db.commit()
                return record
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def search(
        self,
        *,
        query: str,
        thread_id: Optional[str],
        project_id: Optional[str] = None,
        limit: int = 8,
        include_global: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Legacy keyword fallback. Keep this for old code paths and non-vector debugging.
        New planner/router retrieval should prefer vector_candidates() + cosine scoring.
        """
        q = (query or "").strip().lower()
        if not q:
            return []

        terms = [t for t in q.replace("\n", " ").split(" ") if len(t) >= 2][:8]

        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(MemoryModel)

                if terms:
                    conditions = []
                    for term in terms:
                        like = f"%{term}%"
                        conditions.append(MemoryModel.content.ilike(like))
                        conditions.append(MemoryModel.type.ilike(like))
                    stmt = stmt.where(or_(*conditions))

                stmt = self._apply_scope_filters(
                    stmt,
                    thread_id=thread_id,
                    project_id=project_id,
                    include_global=include_global,
                )

                stmt = stmt.order_by(
                    MemoryModel.pinned.desc(),
                    MemoryModel.importance.desc(),
                    MemoryModel.updated_at.desc(),
                ).limit(limit)

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def vector_candidates(
        self,
        *,
        thread_id: Optional[str],
        project_id: Optional[str] = None,
        include_global: bool = True,
        types: Optional[List[str]] = None,
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        """
        Fetch DB-side candidate rows that already have embeddings.

        Similarity scoring is intentionally done outside the repository, because the
        current DB stores embeddings as JSON instead of a native vector type.
        """
        clean_types = [t for t in (types or []) if t]

        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(MemoryModel).where(MemoryModel.embedding.is_not(None))

                stmt = self._apply_scope_filters(
                    stmt,
                    thread_id=thread_id,
                    project_id=project_id,
                    include_global=include_global,
                )

                if clean_types:
                    stmt = stmt.where(MemoryModel.type.in_(clean_types))

                stmt = stmt.order_by(
                    MemoryModel.pinned.desc(),
                    MemoryModel.importance.desc(),
                    MemoryModel.updated_at.desc(),
                ).limit(limit)

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def recent(
        self,
        *,
        thread_id: Optional[str],
        project_id: Optional[str] = None,
        limit: int = 10,
        include_global: bool = False,
    ) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(MemoryModel)

                stmt = self._apply_scope_filters(
                    stmt,
                    thread_id=thread_id,
                    project_id=project_id,
                    include_global=include_global,
                )

                stmt = stmt.order_by(
                    MemoryModel.pinned.desc(),
                    MemoryModel.importance.desc(),
                    MemoryModel.updated_at.desc(),
                ).limit(limit)

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def find_similar_content(self, content: str, limit: int = 3) -> List[Dict[str, Any]]:
        q = (content or "").strip().lower()
        if not q:
            return []

        terms = [t for t in q.replace("\n", " ").split(" ") if len(t) >= 3][:6]

        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(MemoryModel)

                if terms:
                    stmt = stmt.where(
                        or_(*[MemoryModel.content.ilike(f"%{term}%") for term in terms])
                    )

                stmt = stmt.order_by(
                    MemoryModel.importance.desc(),
                    MemoryModel.updated_at.desc(),
                ).limit(limit)

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def records_missing_embeddings(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = (
                    select(MemoryModel)
                    .where(MemoryModel.embedding.is_(None))
                    .order_by(MemoryModel.updated_at.desc())
                    .limit(limit)
                )
                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def update_embedding(
        self,
        *,
        memory_id: str,
        embedding: List[float],
        embedding_model: str,
        embedding_hash: str,
        embedding_updated_at: str,
    ) -> bool:
        def _run() -> bool:
            db = SessionLocal()
            try:
                row = db.get(MemoryModel, memory_id)
                if not row:
                    return False

                row.embedding = embedding
                row.embedding_model = embedding_model
                row.embedding_hash = embedding_hash
                row.embedding_updated_at = embedding_updated_at
                row.updated_at = utc_now_iso()

                db.commit()
                return True
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def delete(self, memory_id: str) -> bool:
        def _run() -> bool:
            db = SessionLocal()
            try:
                row = db.get(MemoryModel, memory_id)
                if not row:
                    return False

                db.delete(row)
                db.commit()
                return True
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    def _apply_scope_filters(
        self,
        stmt,
        *,
        thread_id: Optional[str],
        project_id: Optional[str],
        include_global: bool,
    ):
        scope_filters = []

        if thread_id:
            scope_filters.append(MemoryModel.thread_id == thread_id)

        if project_id:
            scope_filters.append(
                (MemoryModel.scope == "project") & (MemoryModel.project_id == project_id)
            )

        if include_global:
            scope_filters.append(MemoryModel.scope == "global")

        if scope_filters:
            return stmt.where(or_(*scope_filters))

        return stmt.where(MemoryModel.scope == "global")

    def _model_to_dict(self, model: MemoryModel) -> Dict[str, Any]:
        return {
            "id": model.id,
            "type": model.type,
            "content": model.content,
            "scope": model.scope,
            "thread_id": model.thread_id,
            "project_id": model.project_id,
            "importance": model.importance,
            "pinned": bool(model.pinned),
            "metadata_": model.metadata_ or {},
            "embedding": model.embedding,
            "embedding_model": model.embedding_model,
            "embedding_hash": model.embedding_hash,
            "embedding_updated_at": model.embedding_updated_at,
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        }
