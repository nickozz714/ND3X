from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.assistant_output_chunk_repository import (
    AssistantOutputChunkRepository,
)


log = get_logger(__name__)


class AssistantOutputStoreService:
    def __init__(self, db: Session):
        log.infox(
            "AssistantOutputStoreService initialiseren",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        self.repo = AssistantOutputChunkRepository(db)
        log.infox(
            "AssistantOutputStoreService geïnitialiseerd",
            repo_type=type(self.repo).__name__,
        )

    def chunk_text(self, text: str, chunk_size: int = 6000) -> List[str]:
        log.debugx(
            "Assistant output tekst chunken gestart",
            text_length=len(text or ""),
            chunk_size=chunk_size,
        )
        s = (text or "").strip()
        if not s:
            log.debugx(
                "Assistant output tekst chunken afgerond: lege tekst",
                original_length=len(text or ""),
                chunk_count=0,
            )
            return []
        chunks = [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)]
        log.infox(
            "Assistant output tekst chunken afgerond",
            original_length=len(text or ""),
            stripped_length=len(s),
            chunk_size=chunk_size,
            chunk_count=len(chunks),
        )
        return chunks

    def store_text(
        self,
        *,
        text: str,
        session_id: Optional[str],
        turn_id: int,
        assistant_name: str,
        kind: str = "assistant_output",
        chunk_size: int = 6000,
    ) -> Dict[str, object]:
        log.infox(
            "Assistant output tekst opslaan gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            kind=kind,
            text_length=len(text or ""),
            chunk_size=chunk_size,
        )
        chunks = self.chunk_text(text, chunk_size=chunk_size)
        if not chunks:
            log.infox(
                "Assistant output tekst opslaan overgeslagen: geen chunks",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                kind=kind,
            )
            return {
                "id": "",
                "kind": kind,
                "chunk_count": 0,
            }

        output_id = uuid.uuid4().hex
        log.debugx(
            "Assistant output chunks wegschrijven gestart",
            output_id=output_id,
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            kind=kind,
            chunk_count=len(chunks),
        )
        self.repo.create_chunks(
            output_id=output_id,
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            kind=kind,
            chunks=chunks,
        )
        log.infox(
            "Assistant output tekst opslaan afgerond",
            output_id=output_id,
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            kind=kind,
            chunk_count=len(chunks),
        )
        return {
            "id": output_id,
            "kind": kind,
            "chunk_count": len(chunks),
        }

    def retrieve_text(self, *, output_id: str) -> Optional[str]:
        log.infox(
            "Assistant output tekst ophalen gestart",
            output_id=output_id,
        )
        text = self.repo.get_text(output_id=output_id)
        log.infox(
            "Assistant output tekst ophalen afgerond",
            output_id=output_id,
            found=text is not None,
            text_length=len(text or ""),
        )
        return text