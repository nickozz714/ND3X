"""Conversation compaction.

When a thread nears the active model's context window, summarise the conversation
(with the active model, reusing the model-handoff summariser), persist that running
summary, and reset the OpenAI server-side context chain so subsequent turns start
from {summary + recent turns} instead of the full accumulated history. The full
thread is never deleted — only what's *sent* to the model shrinks.
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.token_usage import ThreadCompaction

log = get_logger(__name__)


def latest_compaction_summary(db: Session, thread_id: str) -> Optional[str]:
    row = (
        db.query(ThreadCompaction)
        .filter(ThreadCompaction.thread_id == thread_id)
        .order_by(ThreadCompaction.created_at.desc(), ThreadCompaction.id.desc())
        .first()
    )
    return row.summary if row else None


class CompactionService:
    def __init__(self, db: Session):
        self.db = db

    def _resolve_summary_model(self) -> Optional[str]:
        """A chat model to write the summary with — the thread's final-answer slot,
        else any assigned chat slot. None if no chat model is configured."""
        try:
            from services.providers.registry_service import ProviderRegistryService
            reg = ProviderRegistryService(self.db)
            for slot in ("chat.planner", "chat.cognition"):
                r = reg.resolve_slot(slot)
                if r and r.model_id:
                    return r.model_id
        except Exception as exc:  # noqa: BLE001
            log.warningx("Compaction summary-model resolutie mislukt", error=str(exc))
        return None

    async def compact(self, thread_id: str, openai_service) -> Optional[str]:
        """Summarise the thread, persist it, and reset the OpenAI context chain.
        Returns the summary (or None if nothing to summarise / no chat model)."""
        model = self._resolve_summary_model()
        if not model:
            log.infox("Compaction overgeslagen: geen chat model toegewezen", thread_id=thread_id)
            return None
        try:
            from services.providers.model_handoff import ModelHandoffService
            summary = await ModelHandoffService(self.db).summarize_with_model(
                thread_id, old_model=model, openai_service=openai_service
            )
        except Exception as exc:  # noqa: BLE001 — compaction must never break a turn
            log.warningx("Compaction samenvatting mislukt", thread_id=thread_id, error=str(exc))
            return None
        if not summary:
            return None

        self.db.add(ThreadCompaction(thread_id=thread_id, summary=summary, created_at=time.time()))
        self.db.commit()

        try:
            cleared = openai_service.reset_thread_sessions(thread_id)
            log.infox("Conversatie gecompacteerd", thread_id=thread_id, summary_len=len(summary), sessions_cleared=cleared)
        except Exception as exc:  # noqa: BLE001
            log.warningx("reset_thread_sessions mislukt", thread_id=thread_id, error=str(exc))
        return summary
