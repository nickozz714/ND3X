from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from db.database import SessionLocal
from models.system_cognition import BeliefModel
from services.system_cognition.models import BeliefRecord, utc_now_iso


class BeliefRepository:
    async def upsert(self, record: BeliefRecord) -> BeliefRecord:
        record.updated_at = utc_now_iso()

        def _run() -> BeliefRecord:
            db = SessionLocal()
            try:
                existing = db.get(BeliefModel, record.id)

                if existing:
                    existing.topic = record.topic
                    existing.content = record.content
                    existing.summary = record.summary
                    existing.insights = record.insights or []
                    existing.future_use = record.future_use or []
                    existing.domain = record.domain
                    existing.confidence = float(record.confidence)
                    existing.status = record.status
                    existing.importance = float(record.importance)
                    existing.scope = record.scope
                    existing.thread_id = record.thread_id
                    existing.project_id = record.project_id
                    existing.use_when = record.use_when or []
                    existing.evidence_refs = record.evidence_refs or []
                    existing.contradictions = record.contradictions or []
                    existing.metadata_ = record.metadata_ or {}
                    existing.embedding = getattr(record, "embedding", None)
                    existing.embedding_model = getattr(record, "embedding_model", None)
                    existing.embedding_hash = getattr(record, "embedding_hash", None)
                    existing.embedding_updated_at = getattr(record, "embedding_updated_at", None)
                    existing.updated_at = record.updated_at
                    existing.last_verified_at = record.last_verified_at
                else:
                    db.add(
                        BeliefModel(
                            id=record.id,
                            topic=record.topic,
                            content=record.content,
                            summary=record.summary,
                            insights=record.insights or [],
                            future_use=record.future_use or [],
                            domain=record.domain,
                            confidence=float(record.confidence),
                            status=record.status,
                            importance=float(record.importance),
                            scope=record.scope,
                            thread_id=record.thread_id,
                            project_id=record.project_id,
                            use_when=record.use_when or [],
                            evidence_refs=record.evidence_refs or [],
                            contradictions=record.contradictions or [],
                            metadata_=record.metadata_ or {},
                            embedding=getattr(record, "embedding", None),
                            embedding_model=getattr(record, "embedding_model", None),
                            embedding_hash=getattr(record, "embedding_hash", None),
                            embedding_updated_at=getattr(record, "embedding_updated_at", None),
                            created_at=record.created_at,
                            updated_at=record.updated_at,
                            last_verified_at=record.last_verified_at,
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
        """Legacy keyword fallback. Prefer vector_candidates() for new retrieval."""
        q = (query or "").strip().lower()
        if not q:
            return []

        terms = [t for t in q.replace("\n", " ").split(" ") if len(t) >= 2][:8]

        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(BeliefModel)

                if terms:
                    conditions = []
                    for term in terms:
                        like = f"%{term}%"
                        conditions.append(BeliefModel.topic.ilike(like))
                        conditions.append(BeliefModel.content.ilike(like))
                        conditions.append(BeliefModel.domain.ilike(like))
                        conditions.append(BeliefModel.summary.ilike(like))
                    stmt = stmt.where(or_(*conditions))

                stmt = self._apply_scope_filters(
                    stmt,
                    thread_id=thread_id,
                    project_id=project_id,
                    include_global=include_global,
                )

                stmt = stmt.order_by(
                    BeliefModel.importance.desc(),
                    BeliefModel.confidence.desc(),
                    BeliefModel.updated_at.desc(),
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
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = select(BeliefModel).where(BeliefModel.embedding.is_not(None))

                stmt = self._apply_scope_filters(
                    stmt,
                    thread_id=thread_id,
                    project_id=project_id,
                    include_global=include_global,
                )

                stmt = stmt.order_by(
                    BeliefModel.importance.desc(),
                    BeliefModel.confidence.desc(),
                    BeliefModel.updated_at.desc(),
                ).limit(limit)

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def by_topic(self, topic: str, limit: int = 5) -> List[Dict[str, Any]]:
        topic = (topic or "").strip()
        if not topic:
            return []

        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = (
                    select(BeliefModel)
                    .where(BeliefModel.topic.ilike(topic))
                    .order_by(
                        BeliefModel.confidence.desc(),
                        BeliefModel.importance.desc(),
                        BeliefModel.updated_at.desc(),
                    )
                    .limit(limit)
                )

                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = (
                    select(BeliefModel)
                    .order_by(BeliefModel.updated_at.desc())
                    .limit(limit)
                )
                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def records_missing_embeddings(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            db = SessionLocal()
            try:
                stmt = (
                    select(BeliefModel)
                    .where(BeliefModel.embedding.is_(None))
                    .order_by(BeliefModel.updated_at.desc())
                    .limit(limit)
                )
                return [self._model_to_dict(row) for row in db.execute(stmt).scalars().all()]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def update_embedding(
        self,
        *,
        belief_id: str,
        embedding: List[float],
        embedding_model: str,
        embedding_hash: str,
        embedding_updated_at: str,
    ) -> bool:
        def _run() -> bool:
            db = SessionLocal()
            try:
                row = db.get(BeliefModel, belief_id)
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

    async def delete(self, belief_id: str) -> bool:
        def _run() -> bool:
            db = SessionLocal()
            try:
                row = db.get(BeliefModel, belief_id)
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
            scope_filters.append(BeliefModel.thread_id == thread_id)

        if project_id:
            scope_filters.append(
                (BeliefModel.scope == "project") & (BeliefModel.project_id == project_id)
            )

        if include_global:
            scope_filters.append(BeliefModel.scope == "global")

        if scope_filters:
            return stmt.where(or_(*scope_filters))

        return stmt.where(BeliefModel.scope == "global")

    def _model_to_dict(self, model: BeliefModel) -> Dict[str, Any]:
        return {
            "id": model.id,
            "topic": model.topic,
            "content": model.content,
            "project_id": model.project_id,
            "summary": model.summary,
            "insights": model.insights,
            "future_use": model.future_use,
            "domain": model.domain,
            "confidence": model.confidence,
            "status": model.status,
            "importance": model.importance,
            "scope": model.scope,
            "thread_id": model.thread_id,
            "use_when": model.use_when or [],
            "evidence_refs": model.evidence_refs or [],
            "contradictions": model.contradictions or [],
            "metadata_": model.metadata_ or {},
            "embedding": model.embedding,
            "embedding_model": model.embedding_model,
            "embedding_hash": model.embedding_hash,
            "embedding_updated_at": model.embedding_updated_at,
            "created_at": model.created_at,
            "updated_at": model.updated_at,
            "last_verified_at": model.last_verified_at,
        }
