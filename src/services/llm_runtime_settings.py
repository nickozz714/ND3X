"""LLM runtime behaviour settings (key/value, editable in the AI Models UI).

These control HOW the orchestrator talks to providers, independent of which model a
slot resolves to:

- prompt_caching_enabled — apply provider prompt caching everywhere it is available
  (OpenAI does it automatically; Anthropic needs explicit cache_control breakpoints).
  Default ON: it is the universal, provider-equal way to keep token cost down.
- openai_server_side_session — OpenAI-only optimisation: keep the conversation in the
  Responses server-side session (previous_response_id) and send only the delta, instead
  of replaying the client-side transcript. Default OFF so OpenAI behaves exactly like the
  other providers (transcript + caching); flip it on to A/B the cheaper OpenAI path.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from repository.application_setting_repository import ApplicationSettingRepository
from schemas.application_settings import ApplicationSettingCreate

PROMPT_CACHING_KEY = "llm_prompt_caching_enabled"
OPENAI_SERVER_SIDE_SESSION_KEY = "llm_openai_server_side_session"

# Chat agent-loop budgets (UI-editable). Per chat turn the agent runs a ReAct loop;
# these cap how far it may go so a turn can't run away.
CHAT_MAX_ITERATIONS_KEY = "chat_agent_max_iterations"
CHAT_MAX_TOOL_CALLS_KEY = "chat_agent_max_tool_calls"
CHAT_MAX_SAME_ERROR_REPEATS_KEY = "chat_agent_max_same_error_repeats"
CHAT_MAX_WALL_CLOCK_SECONDS_KEY = "chat_agent_max_wall_clock_seconds"

# Source of truth for defaults — also used to seed the rows so the UI shows them.
DEFAULTS: dict[str, bool] = {
    PROMPT_CACHING_KEY: True,
    OPENAI_SERVER_SIDE_SESSION_KEY: False,
}

# Numeric defaults (kept in sync with component.config chat agent defaults).
NUMERIC_DEFAULTS: dict[str, int] = {
    CHAT_MAX_ITERATIONS_KEY: 12,
    CHAT_MAX_TOOL_CALLS_KEY: 16,
    CHAT_MAX_SAME_ERROR_REPEATS_KEY: 2,
    CHAT_MAX_WALL_CLOCK_SECONDS_KEY: 300,
}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def get_bool(db: Session, key: str) -> bool:
    row = ApplicationSettingRepository(db).get_by_key(key)
    return _as_bool(row.value if row else None, DEFAULTS.get(key, False))


def get_int(db: Session, key: str) -> int:
    row = ApplicationSettingRepository(db).get_by_key(key)
    return _as_int(row.value if row else None, NUMERIC_DEFAULTS.get(key, 1))


def prompt_caching_enabled(db: Session) -> bool:
    return get_bool(db, PROMPT_CACHING_KEY)


def openai_server_side_session_enabled(db: Session) -> bool:
    return get_bool(db, OPENAI_SERVER_SIDE_SESSION_KEY)


def chat_agent_budgets(db: Session) -> dict[str, int]:
    """The UI-editable chat agent-loop budgets, as the override dict the pipeline applies
    (_agent_loop_budgets). wall-clock may be 0 (= no time limit); the others stay >= 1."""
    return {
        "max_iterations": max(1, get_int(db, CHAT_MAX_ITERATIONS_KEY)),
        "max_tool_calls": max(1, get_int(db, CHAT_MAX_TOOL_CALLS_KEY)),
        "max_same_error_repeats": max(1, get_int(db, CHAT_MAX_SAME_ERROR_REPEATS_KEY)),
        "max_wall_clock_seconds": max(0, get_int(db, CHAT_MAX_WALL_CLOCK_SECONDS_KEY)),
    }


def ensure_seeded(db: Session) -> None:
    """Create the rows with their defaults if missing, so they appear in the UI and have
    the intended default (the generic get_from_code would default every bool to True)."""
    repo = ApplicationSettingRepository(db)
    for key, default in DEFAULTS.items():
        if repo.get_by_key(key) is None:
            repo.create(ApplicationSettingCreate(key=key, value="True" if default else "False"))
    for key, idefault in NUMERIC_DEFAULTS.items():
        if repo.get_by_key(key) is None:
            repo.create(ApplicationSettingCreate(key=key, value=str(idefault)))
