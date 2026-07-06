"""
services/text/text_search_service.py
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from db.faiss_store import FaissStore
from db.database import SessionLocal
from repository.text_repository import TextRepository
from services.openai_service import OpenAIResponsesService


@dataclass
class TextHit:
    score: float
    embedding_id: int
    doc_id: int
    file_path: str
    source: str
    start_char: int
    end_char: int
    chunk_text: str


class TextSearchService:
    def __init__(self, *, faiss: FaissStore, openai: OpenAIResponsesService):
        self.faiss = faiss
        self.openai = openai

    def search(self, query: str, *, top_k: int = 5) -> List[TextHit]:
        qvec = self.openai.embed(query)
        ids, scores = self.faiss.search(qvec, top_k=top_k)

        if not ids:
            return []

        score_by_id = {int(i): float(scores[idx]) for idx, i in enumerate(ids)}

        hits: List[TextHit] = []
        for emb_id in [int(x) for x in ids]:
            db = SessionLocal()
            try:
                repo = TextRepository(db)
                row = repo.get_chunk_by_embedding_id(emb_id)
            finally:
                db.close()

            if not row:
                continue

            try:
                full = Path(row["file_path"]).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue

            s0, e0 = int(row["start_char"]), int(row["end_char"])
            hits.append(TextHit(
                score=score_by_id.get(emb_id, 0.0),
                embedding_id=emb_id,
                doc_id=int(row["doc_id"]),
                file_path=row["file_path"],
                source=row["source"],
                start_char=s0,
                end_char=e0,
                chunk_text=full[s0:e0],
            ))

        return hits