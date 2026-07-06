from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from sqlalchemy import func, or_, select

from component.logging import get_logger
from db.database import SessionLocal
from models.system_cognition import BeliefModel, CuriosityJobModel, MemoryModel


log = get_logger(__name__)


class SystemCognitionQueryService:
    async def list_memories(
        self,
        *,
        q: Optional[str] = None,
        type_: Optional[str] = None,
        scope: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition memories ophalen gestart",
            q=q,
            type_=type_,
            scope=scope,
            thread_id=thread_id,
            limit=limit,
            offset=offset,
        )

        def _run():
            log.debugx(
                "System cognition memories database query gestart",
                q=q,
                type_=type_,
                scope=scope,
                thread_id=thread_id,
                limit=limit,
                offset=offset,
            )
            db = SessionLocal()
            try:
                stmt = select(MemoryModel)
                count_stmt = select(func.count()).select_from(MemoryModel)

                filters = []

                if q:
                    like = f"%{q}%"
                    log.debugx(
                        "Memory query filter toevoegen voor zoekterm",
                        q=q,
                        like=like,
                    )
                    filters.append(
                        or_(
                            MemoryModel.content.ilike(like),
                            MemoryModel.type.ilike(like),
                        )
                    )

                if type_:
                    log.debugx(
                        "Memory query filter toevoegen voor type",
                        type_=type_,
                    )
                    filters.append(MemoryModel.type == type_)

                if scope:
                    log.debugx(
                        "Memory query filter toevoegen voor scope",
                        scope=scope,
                    )
                    filters.append(MemoryModel.scope == scope)

                if thread_id:
                    log.debugx(
                        "Memory query filter toevoegen voor thread_id",
                        thread_id=thread_id,
                    )
                    filters.append(MemoryModel.thread_id == thread_id)

                log.debugx(
                    "Memory query filters opgebouwd",
                    filter_count=len(filters),
                )

                if project_id:
                    filters.append(MemoryModel.project_id == project_id)

                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

                total = db.execute(count_stmt).scalar_one()
                log.debugx(
                    "Memory query totaal bepaald",
                    total=total,
                )

                rows = (
                    db.execute(
                        stmt.order_by(
                            MemoryModel.pinned.desc(),
                            MemoryModel.importance.desc(),
                            MemoryModel.updated_at.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                log.debugx(
                    "Memory query rows opgehaald",
                    row_count=len(rows),
                    total=total,
                    limit=limit,
                    offset=offset,
                )

                result = {
                    "items": [self._memory_to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
                log.infox(
                    "System cognition memories database query afgerond",
                    item_count=len(result["items"]),
                    total=total,
                    limit=limit,
                    offset=offset,
                )
                return result
            finally:
                log.debugx("System cognition memories database sessie sluiten")
                db.close()
                log.debugx("System cognition memories database sessie gesloten")

        result = await asyncio.to_thread(_run)
        log.infox(
            "System cognition memories ophalen afgerond",
            item_count=len(result.get("items", [])),
            total=result.get("total"),
            limit=limit,
            offset=offset,
        )
        return result

    async def list_beliefs(
        self,
        *,
        q: Optional[str] = None,
        topic: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = None,
        scope: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition beliefs ophalen gestart",
            q=q,
            topic=topic,
            domain=domain,
            status=status,
            scope=scope,
            limit=limit,
            offset=offset,
        )

        def _run():
            log.debugx(
                "System cognition beliefs database query gestart",
                q=q,
                topic=topic,
                domain=domain,
                status=status,
                scope=scope,
                limit=limit,
                offset=offset,
            )
            db = SessionLocal()
            try:
                stmt = select(BeliefModel)
                count_stmt = select(func.count()).select_from(BeliefModel)

                filters = []

                if q:
                    like = f"%{q}%"
                    log.debugx(
                        "Belief query filter toevoegen voor zoekterm",
                        q=q,
                        like=like,
                    )
                    filters.append(
                        or_(
                            BeliefModel.topic.ilike(like),
                            BeliefModel.content.ilike(like),
                            BeliefModel.domain.ilike(like),
                        )
                    )

                if topic:
                    log.debugx(
                        "Belief query filter toevoegen voor topic",
                        topic=topic,
                    )
                    filters.append(BeliefModel.topic.ilike(f"%{topic}%"))

                if domain:
                    log.debugx(
                        "Belief query filter toevoegen voor domain",
                        domain=domain,
                    )
                    filters.append(BeliefModel.domain == domain)

                if status:
                    log.debugx(
                        "Belief query filter toevoegen voor status",
                        status=status,
                    )
                    filters.append(BeliefModel.status == status)

                if scope:
                    log.debugx(
                        "Belief query filter toevoegen voor scope",
                        scope=scope,
                    )
                    filters.append(BeliefModel.scope == scope)
                if thread_id:
                    filters.append(BeliefModel.thread_id == thread_id)
                if project_id:
                    filters.append(BeliefModel.project_id == project_id)

                log.debugx(
                    "Belief query filters opgebouwd",
                    filter_count=len(filters),
                )
                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

                total = db.execute(count_stmt).scalar_one()
                log.debugx(
                    "Belief query totaal bepaald",
                    total=total,
                )

                rows = (
                    db.execute(
                        stmt.order_by(
                            BeliefModel.importance.desc(),
                            BeliefModel.confidence.desc(),
                            BeliefModel.updated_at.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                log.debugx(
                    "Belief query rows opgehaald",
                    row_count=len(rows),
                    total=total,
                    limit=limit,
                    offset=offset,
                )

                result = {
                    "items": [self._belief_to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
                log.infox(
                    "System cognition beliefs database query afgerond",
                    item_count=len(result["items"]),
                    total=total,
                    limit=limit,
                    offset=offset,
                )
                return result
            finally:
                log.debugx("System cognition beliefs database sessie sluiten")
                db.close()
                log.debugx("System cognition beliefs database sessie gesloten")

        result = await asyncio.to_thread(_run)
        log.infox(
            "System cognition beliefs ophalen afgerond",
            item_count=len(result.get("items", [])),
            total=result.get("total"),
            limit=limit,
            offset=offset,
        )
        return result

    async def list_curiosity_jobs(
        self,
        *,
        q: Optional[str] = None,
        status: Optional[str] = None,
        depth: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        scope: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition curiosity jobs ophalen gestart",
            q=q,
            status=status,
            depth=depth,
            thread_id=thread_id,
            limit=limit,
            offset=offset,
        )

        def _run():
            log.debugx(
                "System cognition curiosity jobs database query gestart",
                q=q,
                status=status,
                depth=depth,
                thread_id=thread_id,
                limit=limit,
                offset=offset,
            )
            db = SessionLocal()
            try:
                stmt = select(CuriosityJobModel)
                count_stmt = select(func.count()).select_from(CuriosityJobModel)

                filters = []

                if q:
                    like = f"%{q}%"
                    log.debugx(
                        "Curiosity job query filter toevoegen voor zoekterm",
                        q=q,
                        like=like,
                    )
                    filters.append(
                        or_(
                            CuriosityJobModel.topic.ilike(like),
                            CuriosityJobModel.reason.ilike(like),
                        )
                    )

                if status:
                    log.debugx(
                        "Curiosity job query filter toevoegen voor status",
                        status=status,
                    )
                    filters.append(CuriosityJobModel.status == status)

                if depth:
                    log.debugx(
                        "Curiosity job query filter toevoegen voor depth",
                        depth=depth,
                    )
                    filters.append(CuriosityJobModel.depth == depth)

                if thread_id:
                    log.debugx(
                        "Curiosity job query filter toevoegen voor thread_id",
                        thread_id=thread_id,
                    )
                    filters.append(CuriosityJobModel.thread_id == thread_id)

                if project_id:
                    filters.append(CuriosityJobModel.project_id == project_id)

                if scope:
                    filters.append(CuriosityJobModel.scope == scope)

                log.debugx(
                    "Curiosity job query filters opgebouwd",
                    filter_count=len(filters),
                )
                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

                total = db.execute(count_stmt).scalar_one()
                log.debugx(
                    "Curiosity job query totaal bepaald",
                    total=total,
                )

                rows = (
                    db.execute(
                        stmt.order_by(
                            CuriosityJobModel.priority.desc(),
                            CuriosityJobModel.updated_at.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                log.debugx(
                    "Curiosity job query rows opgehaald",
                    row_count=len(rows),
                    total=total,
                    limit=limit,
                    offset=offset,
                )

                result = {
                    "items": [self._job_to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
                log.infox(
                    "System cognition curiosity jobs database query afgerond",
                    item_count=len(result["items"]),
                    total=total,
                    limit=limit,
                    offset=offset,
                )
                return result
            finally:
                log.debugx("System cognition curiosity jobs database sessie sluiten")
                db.close()
                log.debugx("System cognition curiosity jobs database sessie gesloten")

        result = await asyncio.to_thread(_run)
        log.infox(
            "System cognition curiosity jobs ophalen afgerond",
            item_count=len(result.get("items", [])),
            total=result.get("total"),
            limit=limit,
            offset=offset,
        )
        return result

    async def get_overview(self) -> Dict[str, Any]:
        log.infox("System cognition overview ophalen gestart")

        def _run():
            log.debugx("System cognition overview database query gestart")
            db = SessionLocal()
            try:
                memories = db.execute(select(func.count()).select_from(MemoryModel)).scalar_one()
                log.debugx(
                    "System cognition overview memory count bepaald",
                    memories=memories,
                )

                beliefs = db.execute(select(func.count()).select_from(BeliefModel)).scalar_one()
                log.debugx(
                    "System cognition overview belief count bepaald",
                    beliefs=beliefs,
                )

                jobs = db.execute(select(func.count()).select_from(CuriosityJobModel)).scalar_one()
                log.debugx(
                    "System cognition overview job count bepaald",
                    curiosity_jobs=jobs,
                )

                queued_jobs = db.execute(
                    select(func.count()).select_from(CuriosityJobModel).where(CuriosityJobModel.status == "queued")
                ).scalar_one()
                log.debugx(
                    "System cognition overview queued job count bepaald",
                    queued_jobs=queued_jobs,
                )

                running_jobs = db.execute(
                    select(func.count()).select_from(CuriosityJobModel).where(CuriosityJobModel.status == "running")
                ).scalar_one()
                log.debugx(
                    "System cognition overview running job count bepaald",
                    running_jobs=running_jobs,
                )

                failed_jobs = db.execute(
                    select(func.count()).select_from(CuriosityJobModel).where(CuriosityJobModel.status == "failed")
                ).scalar_one()
                log.debugx(
                    "System cognition overview failed job count bepaald",
                    failed_jobs=failed_jobs,
                )

                result = {
                    "memories": memories,
                    "beliefs": beliefs,
                    "curiosity_jobs": jobs,
                    "queued_jobs": queued_jobs,
                    "running_jobs": running_jobs,
                    "failed_jobs": failed_jobs,
                }
                log.infox(
                    "System cognition overview database query afgerond",
                    memories=memories,
                    beliefs=beliefs,
                    curiosity_jobs=jobs,
                    queued_jobs=queued_jobs,
                    running_jobs=running_jobs,
                    failed_jobs=failed_jobs,
                )
                return result
            finally:
                log.debugx("System cognition overview database sessie sluiten")
                db.close()
                log.debugx("System cognition overview database sessie gesloten")

        result = await asyncio.to_thread(_run)
        log.infox(
            "System cognition overview ophalen afgerond",
            memories=result.get("memories"),
            beliefs=result.get("beliefs"),
            curiosity_jobs=result.get("curiosity_jobs"),
            queued_jobs=result.get("queued_jobs"),
            running_jobs=result.get("running_jobs"),
            failed_jobs=result.get("failed_jobs"),
        )
        return result

    def _memory_to_dict(self, model: MemoryModel) -> Dict[str, Any]:
        log.debugx(
            "MemoryModel converteren naar dict gestart",
            memory_id=getattr(model, "id", None),
            type=getattr(model, "type", None),
            scope=getattr(model, "scope", None),
            thread_id=getattr(model, "thread_id", None),
            importance=getattr(model, "importance", None),
            pinned=bool(getattr(model, "pinned", False)),
        )
        result = {
            "id": model.id,
            "type": model.type,
            "content": model.content,
            "scope": model.scope,
            "thread_id": model.thread_id,
            "importance": model.importance,
            "pinned": bool(model.pinned),
            "metadata_": model.metadata_ or {},
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        }
        log.debugx(
            "MemoryModel converteren naar dict afgerond",
            memory_id=result["id"],
            metadata_keys=list(result["metadata_"].keys()) if isinstance(result["metadata_"], dict) else None,
        )
        return result

    def _belief_to_dict(self, model: BeliefModel) -> Dict[str, Any]:
        log.debugx(
            "BeliefModel converteren naar dict gestart",
            belief_id=getattr(model, "id", None),
            topic=getattr(model, "topic", None),
            domain=getattr(model, "domain", None),
            status=getattr(model, "status", None),
            confidence=getattr(model, "confidence", None),
            importance=getattr(model, "importance", None),
            scope=getattr(model, "scope", None),
            thread_id=getattr(model, "thread_id", None),
        )
        result = {
            "id": model.id,
            "topic": model.topic,
            "content": model.content,
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
            "created_at": model.created_at,
            "updated_at": model.updated_at,
            "last_verified_at": model.last_verified_at,
        }
        log.debugx(
            "BeliefModel converteren naar dict afgerond",
            belief_id=result["id"],
            use_when_count=len(result["use_when"]) if isinstance(result["use_when"], list) else None,
            evidence_ref_count=len(result["evidence_refs"]) if isinstance(result["evidence_refs"], list) else None,
            contradiction_count=len(result["contradictions"]) if isinstance(result["contradictions"], list) else None,
            metadata_keys=list(result["metadata_"].keys()) if isinstance(result["metadata_"], dict) else None,
        )
        return result

    def _job_to_dict(self, model: CuriosityJobModel) -> Dict[str, Any]:
        log.debugx(
            "CuriosityJobModel converteren naar dict gestart",
            job_id=getattr(model, "id", None),
            topic=getattr(model, "topic", None),
            depth=getattr(model, "depth", None),
            priority=getattr(model, "priority", None),
            status=getattr(model, "status", None),
            thread_id=getattr(model, "thread_id", None),
            attempts=getattr(model, "attempts", None),
            has_error=bool(getattr(model, "error", None)),
        )
        result = {
            "id": model.id,
            "topic": model.topic,
            "reason": model.reason,
            "depth": model.depth,
            "priority": model.priority,
            "status": model.status,
            "thread_id": model.thread_id,
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
        log.debugx(
            "CuriosityJobModel converteren naar dict afgerond",
            job_id=result["id"],
            result_keys=list(result["result"].keys()) if isinstance(result["result"], dict) else None,
            metadata_keys=list(result["metadata_"].keys()) if isinstance(result["metadata_"], dict) else None,
        )
        return result