"""
services/providers/chat_session.py

Per-request chat state for model switching:

- `forced_chat_model`: a ContextVar holding the model the user explicitly picked
  in the chat UI for this request. When set, it OVERRIDES the workbench routing
  slot (the chat picker is authoritative within a chat session). When unset
  (Auto), the capability slot / default applies.

- `_LAST_MODEL_BY_THREAD`: the last model used per chat thread, so we can detect a
  provider switch and trigger a context handoff summary.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Optional

forced_chat_model: ContextVar[Optional[str]] = ContextVar("forced_chat_model", default=None)

_LAST_MODEL_BY_THREAD: Dict[str, str] = {}


def get_last_model(thread_id: str) -> Optional[str]:
    return _LAST_MODEL_BY_THREAD.get(thread_id)


def set_last_model(thread_id: str, model: str) -> None:
    if thread_id and model:
        _LAST_MODEL_BY_THREAD[thread_id] = model
