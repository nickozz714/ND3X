from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.assistant_output_chunk import AssistantOutputChunk


class AssistantOutputChunkRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_chunks(
        self,
        *,
        output_id: str,
        session_id: Optional[str],
        turn_id: int,
        assistant_name: str,
        kind: str,
        chunks: List[str],
    ) -> None:
        rows = [
            AssistantOutputChunk(
                output_id=output_id,
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                chunk_index=i,
                chunk_count=len(chunks),
                kind=kind,
                content=chunk,
            )
            for i, chunk in enumerate(chunks)
        ]
        self.db.add_all(rows)
        self.db.commit()

    def get_chunks(self, *, output_id: str) -> List[AssistantOutputChunk]:
        stmt = (
            select(AssistantOutputChunk)
            .where(AssistantOutputChunk.output_id == output_id)
            .order_by(AssistantOutputChunk.chunk_index.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_text(self, *, output_id: str) -> Optional[str]:
        rows = self.get_chunks(output_id=output_id)
        if not rows:
            return None
        return "".join(r.content for r in rows)