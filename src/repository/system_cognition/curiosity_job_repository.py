from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from sqlalchemy import select

from db.database import SessionLocal
from models.system_cognition import CuriosityJobModel
from services.system_cognition.models import CuriosityJob, utc_now_iso


class CuriosityJobRepository:
    async def enqueue(self, job: CuriosityJob) -> CuriosityJob:
        job.updated_at = utc_now_iso()

        existing = await self.find_recent_same_topic(
            topic=job.topic,
            scope=job.scope,
            thread_id=job.thread_id,
            project_id=job.project_id,
        )
        if existing:
            return CuriosityJob(
                id=existing["id"],
                topic=existing["topic"],
                reason=existing.get("reason") or "",
                depth=existing.get("depth") or "small",
                priority=float(existing.get("priority") or 0.5),
                status=existing.get("status") or "queued",
                scope=existing.get("scope") or "thread",
                thread_id=existing.get("thread_id"),
                project_id=existing.get("project_id"),
                source_question=existing.get("source_question"),
                source_answer=existing.get("source_answer"),
                attempts=int(existing.get("attempts") or 0),
                error=existing.get("error"),
                result=existing.get("result") or {},
                metadata_=existing.get("metadata_") or {},
                created_at=existing.get("created_at"),
                started_at=existing.get("started_at"),
                completed_at=existing.get("completed_at"),
                updated_at=existing.get("updated_at"),
            )

        def _run():
            db = SessionLocal()
            try:
                db.add(
                    CuriosityJobModel(
                        id=job.id,
                        topic=job.topic,
                        reason=job.reason,
                        depth=job.depth,
                        priority=float(job.priority),
                        status=job.status,
                        scope=job.scope,
                        thread_id=job.thread_id,
                        project_id=job.project_id,
                        source_question=job.source_question,
                        source_answer=job.source_answer,
                        attempts=int(job.attempts),
                        error=job.error,
                        result=job.result or {},
                        metadata_=job.metadata_ or {},
                        created_at=job.created_at,
                        started_at=job.started_at,
                        completed_at=job.completed_at,
                        updated_at=job.updated_at,
                    )
                )
                db.commit()
                return job
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def find_recent_same_topic(
            self,
            topic: str,
            *,
            scope: Optional[str] = None,
            thread_id: Optional[str] = None,
            project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        topic = (topic or "").strip()
        if not topic:
            return None

        def _run():
            db = SessionLocal()
            try:
                stmt = (
                    select(CuriosityJobModel)
                    .where(CuriosityJobModel.topic.ilike(topic))
                    .where(CuriosityJobModel.status.in_(["queued", "running", "completed"]))
                )

                if scope:
                    stmt = stmt.where(CuriosityJobModel.scope == scope)

                if scope == "thread" and thread_id:
                    stmt = stmt.where(CuriosityJobModel.thread_id == thread_id)

                if scope == "project" and project_id:
                    stmt = stmt.where(CuriosityJobModel.project_id == project_id)

                if scope == "global":
                    stmt = stmt.where(CuriosityJobModel.thread_id.is_(None))
                    stmt = stmt.where(CuriosityJobModel.project_id.is_(None))

                stmt = (
                    stmt.order_by(CuriosityJobModel.created_at.desc())
                    .limit(1)
                )

                row = db.execute(stmt).scalars().first()
                return self._model_to_dict(row) if row else None
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def claim_next(self) -> Optional[Dict[str, Any]]:
        def _run():
            db = SessionLocal()
            try:
                stmt = (
                    select(CuriosityJobModel)
                    .where(CuriosityJobModel.status == "queued")
                    .order_by(
                        CuriosityJobModel.priority.desc(),
                        CuriosityJobModel.created_at.asc(),
                    )
                    .limit(1)
                )

                row = db.execute(stmt).scalars().first()
                if not row:
                    return None

                now = utc_now_iso()
                row.status = "running"
                row.started_at = now
                row.updated_at = now
                row.attempts = int(row.attempts or 0) + 1

                db.commit()
                db.refresh(row)
                return self._model_to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def mark_completed(self, job_id: str, result: Dict[str, Any]) -> None:
        def _run():
            db = SessionLocal()
            try:
                row = db.get(CuriosityJobModel, job_id)
                if not row:
                    return

                now = utc_now_iso()
                row.status = "completed"
                row.completed_at = now
                row.updated_at = now
                row.result = result or {}
                row.error = None

                db.commit()
            finally:
                db.close()

        await asyncio.to_thread(_run)

    async def mark_failed(self, job_id: str, error: str) -> None:
        def _run():
            db = SessionLocal()
            try:
                row = db.get(CuriosityJobModel, job_id)
                if not row:
                    return

                now = utc_now_iso()
                row.status = "failed"
                row.error = (error or "")[:2000]
                row.updated_at = now

                db.commit()
            finally:
                db.close()

        await asyncio.to_thread(_run)

    async def queued_count(self) -> int:
        def _run():
            db = SessionLocal()
            try:
                stmt = select(CuriosityJobModel).where(CuriosityJobModel.status == "queued")
                return len(db.execute(stmt).scalars().all())
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    def _model_to_dict(self, model: CuriosityJobModel) -> Dict[str, Any]:
        return {
            "id": model.id,
            "topic": model.topic,
            "reason": model.reason,
            "depth": model.depth,
            "priority": model.priority,
            "status": model.status,
            "thread_id": model.thread_id,
            "scope": model.scope,
            "project_id": model.project_id,
            "source_question": model.source_question,
            "source_answer": model.source_answer,
            "attempts": model.attempts,
            "error": model.error,
            "result": model.result or {},
            "metadata_": model.metadata_ or {},
            "created_at": model.created_at,
            "started_at": model.started_at,
            "completed_at": model.completed_at,
            "updated_at": model.updated_at,
        }

    async def delete(self, job_id: str) -> bool:
        def _run() -> bool:
            db = SessionLocal()
            try:
                row = db.get(CuriosityJobModel, job_id)
                if not row:
                    return False

                db.delete(row)
                db.commit()
                return True
            finally:
                db.close()

        return await asyncio.to_thread(_run)