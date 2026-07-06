"""
repository/embedding_repository.py
"""
from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from models.text_document import EmbeddingSeq


class EmbeddingSeqRepository:
    def __init__(self, db: Session):
        self.db = db

    def _ensure(self) -> EmbeddingSeq:
        seq = self.db.query(EmbeddingSeq).filter(EmbeddingSeq.name == "global").first()
        if not seq:
            seq = EmbeddingSeq(name="global", next_id=0)
            self.db.add(seq)
            self.db.flush()
        return seq

    def allocate(self, n: int) -> List[int]:
        if n <= 0:
            return []
        seq = self._ensure()
        start = int(seq.next_id)
        seq.next_id = start + n
        self.db.flush()
        return list(range(start, start + n))