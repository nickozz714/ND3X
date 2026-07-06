"""LLM runtime behaviour settings: defaults (caching ON, OpenAI session OFF) + seeding."""
from __future__ import annotations

import services.llm_runtime_settings as lrs


class _Row:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeRepo:
    def __init__(self, store: dict) -> None:
        self.store = store

    def get_by_key(self, key: str):
        v = self.store.get(key)
        return _Row(v) if v is not None else None

    def create(self, data):
        self.store[data.key] = data.value
        return _Row(data.value)


def test_defaults_and_seeding(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(lrs, "ApplicationSettingRepository", lambda db: _FakeRepo(store))

    # Defaults when nothing is stored yet.
    assert lrs.prompt_caching_enabled(None) is True
    assert lrs.openai_server_side_session_enabled(None) is False

    # Seeding writes the intended defaults (not "True" for both).
    lrs.ensure_seeded(None)
    assert store[lrs.PROMPT_CACHING_KEY] == "True"
    assert store[lrs.OPENAI_SERVER_SIDE_SESSION_KEY] == "False"

    # Stored values are honoured (the toggle works).
    store[lrs.OPENAI_SERVER_SIDE_SESSION_KEY] = "True"
    assert lrs.openai_server_side_session_enabled(None) is True
    store[lrs.PROMPT_CACHING_KEY] = "False"
    assert lrs.prompt_caching_enabled(None) is False


def test_chat_agent_budgets_defaults_seeding_and_override(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(lrs, "ApplicationSettingRepository", lambda db: _FakeRepo(store))

    # Defaults before seeding.
    b = lrs.chat_agent_budgets(None)
    assert b["max_iterations"] == 12 and b["max_tool_calls"] == 16
    assert b["max_same_error_repeats"] == 2 and b["max_wall_clock_seconds"] == 300

    # Seeding writes the numeric defaults as strings.
    lrs.ensure_seeded(None)
    assert store[lrs.CHAT_MAX_ITERATIONS_KEY] == "12"
    assert store[lrs.CHAT_MAX_WALL_CLOCK_SECONDS_KEY] == "300"

    # Edited values are honoured; wall-clock may be 0, others floored at 1.
    store[lrs.CHAT_MAX_ITERATIONS_KEY] = "25"
    store[lrs.CHAT_MAX_WALL_CLOCK_SECONDS_KEY] = "0"
    store[lrs.CHAT_MAX_TOOL_CALLS_KEY] = "0"
    b = lrs.chat_agent_budgets(None)
    assert b["max_iterations"] == 25
    assert b["max_wall_clock_seconds"] == 0
    assert b["max_tool_calls"] == 1  # floored
