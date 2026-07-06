from __future__ import annotations

import asyncio
from typing import Dict, Iterable, Set
from uuid import uuid4

from sqlalchemy import delete, select

from db.database import SessionLocal
from models.system_cognition import MemoryInjectionModel
from services.system_cognition.models import utc_now_iso


class MemoryInjectionRepository:
    async def get_injected_ids(
        self,
        *,
        thread_id: str,
    ) -> Dict[str, Set[str]]:
        def _run() -> Dict[str, Set[str]]:
            db = SessionLocal()
            try:
                rows = (
                    db.execute(
                        select(MemoryInjectionModel)
                        .where(MemoryInjectionModel.thread_id == thread_id)
                    )
                    .scalars()
                    .all()
                )

                result: Dict[str, Set[str]] = {
                    "memory": set(),
                    "belief": set(),
                }

                for row in rows:
                    kind = row.memory_kind
                    if kind not in result:
                        result[kind] = set()
                    result[kind].add(row.memory_id)

                return result
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def mark_injected(
        self,
        *,
        thread_id: str,
        items: Iterable[Dict[str, str]],
    ) -> int:
        """
        items shape:
        [
            {"memory_kind": "memory", "memory_id": "..."},
            {"memory_kind": "belief", "memory_id": "..."}
        ]

        Duplicate inserts are ignored.
        """
        now = utc_now_iso()

        def _run() -> int:
            db = SessionLocal()
            inserted = 0

            try:
                for item in items:
                    memory_kind = (item.get("memory_kind") or "").strip()
                    memory_id = (item.get("memory_id") or "").strip()

                    if memory_kind not in {"memory", "belief"}:
                        continue

                    if not memory_id:
                        continue

                    exists = db.execute(
                        select(MemoryInjectionModel.id)
                        .where(MemoryInjectionModel.thread_id == thread_id)
                        .where(MemoryInjectionModel.memory_kind == memory_kind)
                        .where(MemoryInjectionModel.memory_id == memory_id)
                        .limit(1)
                    ).scalar_one_or_none()

                    if exists:
                        continue

                    db.add(
                        MemoryInjectionModel(
                            id=str(uuid4()),
                            thread_id=thread_id,
                            memory_kind=memory_kind,
                            memory_id=memory_id,
                            created_at=now,
                        )
                    )
                    inserted += 1

                db.commit()
                return inserted

            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def delete_for_memory(
        self,
        *,
        memory_kind: str,
        memory_id: str,
    ) -> int:
        def _run() -> int:
            db = SessionLocal()
            try:
                result = db.execute(
                    delete(MemoryInjectionModel)
                    .where(MemoryInjectionModel.memory_kind == memory_kind)
                    .where(MemoryInjectionModel.memory_id == memory_id)
                )
                db.commit()
                return int(result.rowcount or 0)
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def delete_for_thread(
        self,
        *,
        thread_id: str,
    ) -> int:
        def _run() -> int:
            db = SessionLocal()
            try:
                result = db.execute(
                    delete(MemoryInjectionModel)
                    .where(MemoryInjectionModel.thread_id == thread_id)
                )
                db.commit()
                return int(result.rowcount or 0)
            finally:
                db.close()

        return await asyncio.to_thread(_run)