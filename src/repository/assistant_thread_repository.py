# repository/assistant_thread_repository.py

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import func, select

from db.database import SessionLocal
from models.assistant_thread import AssistantThreadModel, AssistantThreadMessageModel
from services.system_cognition.models import utc_now_iso


def _delete_thread_cognition(db, thread_id: str) -> Dict[str, int]:
    """Delete thread-scoped cognition rows. Returns per-table counts."""
    from models.system_cognition import MemoryModel, BeliefModel, CuriosityJobModel
    out = {"memories": 0, "beliefs": 0, "curiosity_jobs": 0}
    out["memories"] = db.query(MemoryModel).filter(
        MemoryModel.thread_id == thread_id).delete(synchronize_session=False)
    out["beliefs"] = db.query(BeliefModel).filter(
        BeliefModel.thread_id == thread_id).delete(synchronize_session=False)
    out["curiosity_jobs"] = db.query(CuriosityJobModel).filter(
        CuriosityJobModel.thread_id == thread_id).delete(synchronize_session=False)
    return out


def _delete_project_cognition(db, project_id: str) -> Dict[str, int]:
    """Delete project-scoped cognition rows. Returns per-table counts."""
    from models.system_cognition import MemoryModel, BeliefModel, CuriosityJobModel
    out = {"memories": 0, "beliefs": 0, "curiosity_jobs": 0}
    out["memories"] = db.query(MemoryModel).filter(
        MemoryModel.project_id == project_id).delete(synchronize_session=False)
    out["beliefs"] = db.query(BeliefModel).filter(
        BeliefModel.project_id == project_id).delete(synchronize_session=False)
    out["curiosity_jobs"] = db.query(CuriosityJobModel).filter(
        CuriosityJobModel.project_id == project_id).delete(synchronize_session=False)
    return out


class AssistantThreadRepository:
    async def ensure_thread(
        self,
        *,
        thread_id: str,
        project_id: Optional[str] = None,
        title: Optional[str] = None,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadModel, thread_id)

                if row:
                    if project_id and not row.project_id:
                        row.project_id = project_id
                    if title and not row.title:
                        row.title = title[:255]

                    if metadata_:
                        current = row.metadata_ or {}
                        current.update(metadata_)
                        row.metadata_ = current

                    row.updated_at = now
                    row.last_turn_at = now
                    db.commit()
                    db.refresh(row)
                    return self._thread_to_dict(row)

                row = AssistantThreadModel(
                    id=thread_id,
                    title=(title or "New thread")[:255],
                    summary=None,
                    project_id=project_id,
                    status="active",
                    is_archived=False,
                    metadata_=metadata_ or {},
                    created_at=now,
                    updated_at=now,
                    last_turn_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                return self._thread_to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadModel, thread_id)
                return self._thread_to_dict(row) if row else None
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def list_threads(
        self,
        *,
        project_id: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        def _run():
            db = SessionLocal()
            try:
                stmt = select(AssistantThreadModel)
                count_stmt = select(func.count()).select_from(AssistantThreadModel)

                filters = []

                if project_id:
                    filters.append(AssistantThreadModel.project_id == project_id)

                if not include_archived:
                    filters.append(AssistantThreadModel.is_archived.is_(False))

                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

                total = db.execute(count_stmt).scalar_one()

                rows = (
                    db.execute(
                        stmt.order_by(AssistantThreadModel.updated_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                return {
                    "items": [self._thread_to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def update_thread(
        self,
        *,
        thread_id: str,
        title: Optional[str] = None,
        project_id: Optional[str] = None,
        project_id_provided: bool = False,
        status: Optional[str] = None,
        is_archived: Optional[bool] = None,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadModel, thread_id)
                if not row:
                    return None

                if title is not None:
                    row.title = title[:255]

                if project_id_provided:
                    row.project_id = project_id

                if status is not None:
                    row.status = status

                if is_archived is not None:
                    row.is_archived = bool(is_archived)

                if metadata_ is not None:
                    current = row.metadata_ or {}
                    current.update(metadata_)
                    row.metadata_ = current

                row.updated_at = now

                db.commit()
                db.refresh(row)

                return self._thread_to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def add_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        turn_id: Optional[int] = None,
        steps: Optional[list] = None,
    ) -> Dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("Only role='user' and role='assistant' are allowed.")

        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                max_seq = (
                    db.execute(
                        select(func.max(AssistantThreadMessageModel.sequence))
                        .where(AssistantThreadMessageModel.thread_id == thread_id)
                    ).scalar()
                    or 0
                )

                row = AssistantThreadMessageModel(
                    id=str(uuid4()),
                    thread_id=thread_id,
                    role=role,
                    content=content or "",
                    turn_id=turn_id,
                    sequence=int(max_seq) + 1,
                    created_at=now,
                    steps=steps or None,
                )
                db.add(row)

                thread = db.get(AssistantThreadModel, thread_id)
                if thread:
                    thread.updated_at = now
                    thread.last_turn_at = now

                db.commit()
                db.refresh(row)
                return self._message_to_dict(row)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def list_messages(
        self,
        *,
        thread_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        def _run():
            db = SessionLocal()
            try:
                total = db.execute(
                    select(func.count())
                    .select_from(AssistantThreadMessageModel)
                    .where(AssistantThreadMessageModel.thread_id == thread_id)
                ).scalar_one()

                rows = (
                    db.execute(
                        select(AssistantThreadMessageModel)
                        .where(AssistantThreadMessageModel.thread_id == thread_id)
                        .order_by(AssistantThreadMessageModel.sequence.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )

                return {
                    "items": [self._message_to_dict(row) for row in rows],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def update_summary(self, *, thread_id: str, summary: str) -> None:
        now = utc_now_iso()

        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadModel, thread_id)
                if not row:
                    return
                row.summary = summary
                row.updated_at = now
                db.commit()
            finally:
                db.close()

        await asyncio.to_thread(_run)

    async def delete_thread(self, *, thread_id: str, delete_memories: bool = False) -> Optional[Dict[str, int]]:
        """Delete a thread + its messages (and compaction rows). When
        delete_memories is set, also delete the thread-scoped cognition
        (memories, beliefs, curiosity jobs). Returns deletion counts, or None if
        the thread does not exist."""
        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadModel, thread_id)
                if not row:
                    return None
                counts = {"messages": 0, "memories": 0, "beliefs": 0, "curiosity_jobs": 0}
                counts["messages"] = (
                    db.query(AssistantThreadMessageModel)
                    .filter(AssistantThreadMessageModel.thread_id == thread_id)
                    .delete(synchronize_session=False)
                )
                try:
                    from models.token_usage import ThreadCompaction
                    db.query(ThreadCompaction).filter(
                        ThreadCompaction.thread_id == thread_id
                    ).delete(synchronize_session=False)
                except Exception:
                    pass
                if delete_memories:
                    counts.update(_delete_thread_cognition(db, thread_id))
                db.delete(row)
                db.commit()
                return counts
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    def _thread_to_dict(self, row: AssistantThreadModel) -> Dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "summary": row.summary,
            "project_id": row.project_id,
            "status": row.status,
            "is_archived": bool(row.is_archived),
            "metadata_": row.metadata_ or {},
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "last_turn_at": row.last_turn_at,
        }

    def _message_to_dict(self, row: AssistantThreadMessageModel) -> Dict[str, Any]:
        return {
            "id": row.id,
            "thread_id": row.thread_id,
            "role": row.role,
            "content": row.content,
            "turn_id": row.turn_id,
            "sequence": row.sequence,
            "important": bool(getattr(row, "important", False)),
            "steps": getattr(row, "steps", None) or None,
            "created_at": row.created_at,
        }

    async def set_message_important(
        self, *, thread_id: str, message_id: str, important: bool
    ) -> Optional[Dict[str, Any]]:
        """Toggle a message's `important` flag. Returns context for a forced
        cognition pass ({message, question, answer, turn_id, project_id}) or None
        if the message does not exist in the thread."""
        def _run():
            db = SessionLocal()
            try:
                row = db.get(AssistantThreadMessageModel, message_id)
                if not row or row.thread_id != thread_id:
                    return None
                row.important = bool(important)
                db.commit()
                db.refresh(row)

                # Build the turn's question/answer for cognition.
                question = row.content if row.role == "user" else ""
                answer = row.content if row.role == "assistant" else ""
                if row.turn_id is not None:
                    turn_msgs = (
                        db.query(AssistantThreadMessageModel)
                        .filter(
                            AssistantThreadMessageModel.thread_id == thread_id,
                            AssistantThreadMessageModel.turn_id == row.turn_id,
                        )
                        .order_by(AssistantThreadMessageModel.sequence.asc())
                        .all()
                    )
                    for m in turn_msgs:
                        if m.role == "user" and not question:
                            question = m.content
                        if m.role == "assistant" and not answer:
                            answer = m.content
                thread = db.get(AssistantThreadModel, thread_id)
                return {
                    "message": self._message_to_dict(row),
                    "question": question or row.content,
                    "answer": answer or "",
                    "turn_id": row.turn_id or 0,
                    "project_id": thread.project_id if thread else None,
                }
            finally:
                db.close()

        return await asyncio.to_thread(_run)