"""
services/providers/model_handoff.py

When a chat session switches to a model on a DIFFERENT provider, the previously
active model writes a concise summary of the conversation so the new model can
pick up with that context loaded. (Same-provider switches don't need it.)
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.provider import Provider, ProviderModel
from services.providers.chat_session import get_last_model, set_last_model
from services.providers.openai_provider import OpenAIChatProvider
from services.providers.provider_factory import _build_chat_provider
from services.providers.registry_service import ProviderRegistryService

log = get_logger(__name__)

_HANDOFF_INSTRUCTIONS = (
    "You are handing this conversation off to a different AI model. Write a concise but "
    "complete summary so the next model can continue seamlessly. Cover: the user's goal, "
    "the key facts and decisions so far, what has already been done, and any open questions "
    "or next steps. Plain text, no preamble."
)

_HISTORY_LIMIT = 40
_MAX_CHARS = 12000


class ModelHandoffService:
    def __init__(self, db: Session):
        self.db = db
        self.reg = ProviderRegistryService(db)

    def provider_type_of(self, model: str) -> str:
        """Provider type for a model id. Unregistered models are treated as
        'openai' (the built-in gpt defaults)."""
        m = (
            self.db.query(ProviderModel)
            .join(Provider, Provider.id == ProviderModel.provider_id)
            .filter(ProviderModel.model_id == model, ProviderModel.capability == "chat")
            .first()
        )
        if m and m.provider:
            return m.provider.provider_type or "openai"
        return "openai"

    def _chat_provider_for_model(self, model: str, openai_service: Any):
        ptype = self.provider_type_of(model)
        if ptype == "openai":
            return OpenAIChatProvider(openai_service)
        pm = (
            self.db.query(ProviderModel)
            .filter(ProviderModel.model_id == model, ProviderModel.capability == "chat")
            .first()
        )
        if not pm:
            return OpenAIChatProvider(openai_service)
        p = self.reg.get_provider(pm.provider_id)
        if not p:
            return OpenAIChatProvider(openai_service)
        return _build_chat_provider(p, self.reg.get_api_key(p.id), model, openai_service)

    async def _transcript(self, thread_id: str) -> str:
        from services.assistant_thread_service import AssistantThreadService
        result = await AssistantThreadService().list_messages(thread_id=thread_id, limit=_HISTORY_LIMIT, offset=0)
        lines: List[str] = []
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = (item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                lines.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
        text = "\n\n".join(lines)
        return text[-_MAX_CHARS:]

    async def summarize_with_model(
        self,
        thread_id: str,
        *,
        old_model: str,
        openai_service: Any,
        prior_summary: Optional[str] = None,
    ) -> Optional[str]:
        transcript = await self._transcript(thread_id)
        # With a prior summary we can still produce an updated one even if the
        # recent transcript window is empty; without either there's nothing to do.
        if not transcript.strip() and not (prior_summary or "").strip():
            return None
        provider = self._chat_provider_for_model(old_model, openai_service)
        if provider is None:
            return None
        # Build on the previously persisted summary instead of re-deriving the
        # whole conversation from scratch on every switch.
        if prior_summary and prior_summary.strip():
            prompt = (
                f"Earlier summary of this conversation:\n\n{prior_summary.strip()}\n\n"
                f"Conversation since then:\n\n{transcript}"
            )
        else:
            prompt = f"Conversation so far:\n\n{transcript}"
        try:
            res = await provider.chat(
                prompt,
                model=old_model,
                instructions=_HANDOFF_INSTRUCTIONS,
                max_output_tokens=1200,
            )
            return (res.text or "").strip() or None
        except Exception as exc:  # noqa: BLE001 — a failed summary must not break the turn
            log.warningx("Model handoff summary mislukt", old_model=old_model, error=str(exc))
            return None


async def handle_model_switch(thread_id: str, new_model: str, openai_service: Any, *, db) -> Optional[str]:
    """If the thread's previous model used a different provider than `new_model`,
    return a handoff summary written by the OLD model. Always records the new model
    as the thread's current model.

    The summary is persisted (reusing the ThreadCompaction store) and seeds the
    next switch, so repeated provider switches build on the prior summary instead
    of re-summarising the whole conversation from scratch each time."""
    import time

    from models.token_usage import ThreadCompaction
    from services.compaction_service import latest_compaction_summary

    svc = ModelHandoffService(db)
    last = get_last_model(thread_id)
    set_last_model(thread_id, new_model)
    if not last or last == new_model:
        return None
    if svc.provider_type_of(last) == svc.provider_type_of(new_model):
        return None  # same provider — shared message history is enough
    log.infox("Model provider switch — handoff summary", thread_id=thread_id, old=last, new=new_model)

    prior_summary = None
    try:
        prior_summary = latest_compaction_summary(db, thread_id)
    except Exception:  # noqa: BLE001 — a missing prior summary just means start fresh
        prior_summary = None

    summary = await svc.summarize_with_model(
        thread_id, old_model=last, openai_service=openai_service, prior_summary=prior_summary,
    )
    if summary:
        try:
            db.add(ThreadCompaction(thread_id=thread_id, summary=summary, created_at=time.time()))
            db.commit()
        except Exception as exc:  # noqa: BLE001 — persistence must never break the turn
            log.warningx("Handoff summary persist mislukt", thread_id=thread_id, error=str(exc))
            db.rollback()
    return summary
