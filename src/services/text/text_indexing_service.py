"""
services/text/text_indexing_service.py
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Union

from db.faiss_store import FaissStore
from db.database import SessionLocal
from repository.text_repository import TextRepository
from repository.embedding_repository import EmbeddingSeqRepository
from services.text.text_storage_service import TextStorageService, IncomingText, IncomingCode
from services.openai_service import OpenAIResponsesService
from component.logging import get_logger

log = get_logger(__name__)


def chunk_text(text: str, *, max_chars: int = 1800, overlap: int = 200) -> List[Tuple[int, int]]:
    text = text or ""
    n = len(text)
    if n == 0:
        return []
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + max_chars, n)
        spans.append((start, end))
        if end == n:
            break
        start = max(0, end - overlap)
    return spans


class TextIndexingService:
    def __init__(
        self,
        *,
        faiss: FaissStore,
        storage: TextStorageService,
        openai: OpenAIResponsesService,
    ):
        self.faiss = faiss
        self.storage = storage
        self.openai = openai

    def get_doc(self, *, doc_id: int, include_content: bool = True) -> dict:
        db = SessionLocal()
        try:
            repo = TextRepository(db)
            doc = repo.get_doc_by_id(doc_id)
        finally:
            db.close()

        if not doc:
            return {"ok": False, "error": "Doc not found", "doc_id": doc_id}

        content = None
        if include_content:
            try:
                content = Path(doc["file_path"]).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                content = None

        return {
            "ok": True,
            "doc": {
                "doc_id": int(doc["id"]),
                "source": doc["source"],
                "file_path": doc["file_path"],
                "created_at": doc["created_at"],
                "content": content,
            },
        }

    def ingest_text(self, item: Union[IncomingText, IncomingCode]) -> dict:
        if isinstance(item, IncomingText):
            path = self.storage.save_markdown(item)
        else:
            path = self.storage.save_code(item)

        text = path.read_text(encoding="utf-8", errors="replace")
        spans = chunk_text(text)
        chunks = [(idx, s, e, text[s:e].strip()) for idx, (s, e) in enumerate(spans) if text[s:e].strip()]

        db = SessionLocal()
        try:
            text_repo = TextRepository(db)
            embseq = EmbeddingSeqRepository(db)

            doc_obj = text_repo.insert_doc(source=item.source, file_path=str(path.resolve()))
            doc_id = int(doc_obj.id)

            embedding_ids = embseq.allocate(len(chunks))

            for alloc_id, (idx, s0, e0, chunk) in zip(embedding_ids, chunks):
                vec = self.openai.embed(chunk)
                self.faiss.add(vec, embedding_id=int(alloc_id))
                text_repo.insert_chunk(
                    doc_id=doc_id,
                    chunk_index=idx,
                    start_char=s0,
                    end_char=e0,
                    embedding_id=int(alloc_id),
                )

            db.commit()
        finally:
            db.close()

        self.faiss.persist()

        return {
            "ok": True,
            "doc_id": doc_id,
            "file_path": str(path),
            "chunks_indexed": len(chunks),
        }

    def delete_doc(self, *, doc_id: int, delete_file: bool = True) -> dict:
        db = SessionLocal()
        try:
            text_repo = TextRepository(db)
            doc = text_repo.get_doc_by_id(doc_id)
            if not doc:
                return {"ok": False, "error": "Doc not found", "doc_id": doc_id}
            emb_ids = text_repo.list_chunk_embedding_ids_for_doc(doc_id)
            removed = self.faiss.remove_ids([int(x) for x in emb_ids])
            self.faiss.persist()
            text_repo.delete_chunks_for_doc(doc_id)
            text_repo.delete_doc(doc_id)
            db.commit()
        finally:
            db.close()

        if delete_file:
            try:
                Path(doc["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass

        return {"ok": True, "doc_id": doc_id, "removed_vectors": removed, "deleted_file": bool(delete_file)}

    def update_doc_content(self, *, doc_id: int, new_content: str) -> dict:
        db = SessionLocal()
        try:
            text_repo = TextRepository(db)
            doc = text_repo.get_doc_by_id(doc_id)
            if not doc:
                return {"ok": False, "error": "Doc not found", "doc_id": doc_id}

            old_ids = text_repo.list_chunk_embedding_ids_for_doc(doc_id)
            removed = self.faiss.remove_ids([int(x) for x in old_ids])
            self.faiss.persist()

            fp = Path(doc["file_path"])
            fp.write_text(new_content, encoding="utf-8")
            text = fp.read_text(encoding="utf-8", errors="replace")
            spans = chunk_text(text)
            chunks = [(idx, s, e, text[s:e].strip()) for idx, (s, e) in enumerate(spans) if text[s:e].strip()]

            embseq = EmbeddingSeqRepository(db)
            text_repo.delete_chunks_for_doc(doc_id)
            new_ids = embseq.allocate(len(chunks))

            for alloc_id, (idx, s0, e0, chunk) in zip(new_ids, chunks):
                vec = self.openai.embed(chunk)
                self.faiss.add(vec, embedding_id=int(alloc_id))
                text_repo.insert_chunk(
                    doc_id=doc_id,
                    chunk_index=idx,
                    start_char=s0,
                    end_char=e0,
                    embedding_id=int(alloc_id),
                )

            db.commit()
        finally:
            db.close()

        self.faiss.persist()

        return {
            "ok": True,
            "doc_id": doc_id,
            "file_path": str(fp),
            "chunks_indexed": len(chunks),
            "removed_vectors": removed,
        }