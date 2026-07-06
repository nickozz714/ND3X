"""
repository/text_repository.py
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models.text_document import TextDocument, TextChunk



class TextRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── Documents ─────────────────────────────────────────────────────────────

    def insert_doc(self, *, source: str, file_path: str) -> TextDocument:
        obj = TextDocument(
            source=source,
            file_path=file_path,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(obj)
        self.db.flush()
        return obj

    def get_doc_by_id(self, doc_id: int) -> Optional[Dict[str, Any]]:
        obj = self.db.query(TextDocument).filter(TextDocument.id == doc_id).first()
        if not obj:
            return None
        return {
            "id": obj.id,
            "source": obj.source,
            "file_path": obj.file_path,
            "created_at": obj.created_at,
        }

    def get_doc_by_file_path(self, file_path: str) -> Optional[Dict[str, Any]]:
        obj = self.db.query(TextDocument).filter(TextDocument.file_path == file_path).first()
        if not obj:
            return None
        return {"id": obj.id, "source": obj.source, "file_path": obj.file_path, "created_at": obj.created_at}

    def delete_doc(self, doc_id: int) -> None:
        self.db.query(TextDocument).filter(TextDocument.id == doc_id).delete()
        self.db.flush()

    # ── Chunks ────────────────────────────────────────────────────────────────

    def insert_chunk(
        self,
        *,
        doc_id: int,
        chunk_index: int,
        start_char: int,
        end_char: int,
        embedding_id: int,
    ) -> TextChunk:
        obj = TextChunk(
            doc_id=doc_id,
            chunk_index=chunk_index,
            start_char=start_char,
            end_char=end_char,
            embedding_id=embedding_id,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(obj)
        self.db.flush()
        return obj

    def get_chunk_by_embedding_id(self, embedding_id: int) -> Optional[Dict[str, Any]]:
        chunk = self.db.query(TextChunk).filter(TextChunk.embedding_id == embedding_id).first()
        if not chunk:
            return None
        doc = self.db.query(TextDocument).filter(TextDocument.id == chunk.doc_id).first()
        if not doc:
            return None
        return {
            "embedding_id": chunk.embedding_id,
            "doc_id": chunk.doc_id,
            "chunk_index": chunk.chunk_index,
            "start_char": chunk.start_char,
            "end_char": chunk.end_char,
            "file_path": doc.file_path,
            "source": doc.source,
        }

    def list_chunk_embedding_ids_for_doc(self, doc_id: int) -> List[int]:
        rows = self.db.query(TextChunk.embedding_id).filter(TextChunk.doc_id == doc_id).all()
        return [r[0] for r in rows]

    def delete_chunks_for_doc(self, doc_id: int) -> None:
        self.db.query(TextChunk).filter(TextChunk.doc_id == doc_id).delete()
        self.db.flush()