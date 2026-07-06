"""Conversation compaction: chain reset + summarise/persist."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.token_usage import ThreadCompaction
from services.compaction_service import CompactionService, latest_compaction_summary
from services.openai_service import OpenAIResponsesService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[ThreadCompaction.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def test_reset_thread_sessions_clears_only_that_thread():
    svc = OpenAIResponsesService()  # lazy; no client built
    svc._last_id_by_session = {"tA": "r1", "cognition:tA": "r2", "tB": "r3"}
    cleared = svc.reset_thread_sessions("tA")
    assert cleared == 2
    assert svc._last_id_by_session == {"tB": "r3"}


class _FakeOpenAI:
    def __init__(self):
        self.reset_called = []

    def reset_thread_sessions(self, thread_id):
        self.reset_called.append(thread_id)
        return 1


class _FakeHandoff:
    def __init__(self, _db):
        pass

    async def summarize_with_model(self, thread_id, *, old_model, openai_service):
        return "SUMMARY of the conversation"


def test_compact_summarizes_persists_and_resets(monkeypatch, db):
    monkeypatch.setattr("services.providers.model_handoff.ModelHandoffService", _FakeHandoff)
    svc = CompactionService(db)
    monkeypatch.setattr(svc, "_resolve_summary_model", lambda: "gpt-x")
    fake = _FakeOpenAI()

    summary = asyncio.run(svc.compact("t1", fake))

    assert summary == "SUMMARY of the conversation"
    assert fake.reset_called == ["t1"]                      # chain reset
    assert latest_compaction_summary(db, "t1") == summary   # persisted + readable


def test_compact_noop_without_chat_model(monkeypatch, db):
    svc = CompactionService(db)
    monkeypatch.setattr(svc, "_resolve_summary_model", lambda: None)
    assert asyncio.run(svc.compact("t1", _FakeOpenAI())) is None
    assert latest_compaction_summary(db, "t1") is None
