"""Tests for chat model switching + provider-switch handoff summary."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.providers.model_handoff import ModelHandoffService, handle_model_switch
from services.providers.registry_service import ProviderRegistryService
from schemas.provider import ProviderCreate, ProviderModelCreate


class FakeOpenAI:
    default_model = "gpt-4.1-mini"
    default_embedding_model = "text-embedding-3-small"

    async def ask_async(self, *a, **k):
        return type("R", (), {"text": "HANDOFF SUMMARY", "response_id": "1", "raw": None})()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for m in (pv.Provider, pv.ProviderModel, pv.CapabilityAssignment):
        m.__table__.create(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def ollama_registered(db):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="qwen2.5:14b", capability="chat"))
    return reg


def test_provider_type_of(db, ollama_registered):
    svc = ModelHandoffService(db)
    assert svc.provider_type_of("qwen2.5:14b") == "ollama"
    assert svc.provider_type_of("gpt-4.1-mini") == "openai"   # unregistered default
    assert svc.provider_type_of("anything-else") == "openai"


def _patch_history(monkeypatch, items):
    async def fake_list(self, *, thread_id, limit, offset):
        return {"items": items}
    monkeypatch.setattr(
        "services.assistant_thread_service.AssistantThreadService.list_messages",
        fake_list,
    )


def test_no_summary_on_first_model_and_records_it(db, ollama_registered, monkeypatch):
    from services.providers.chat_session import _LAST_MODEL_BY_THREAD, get_last_model
    _LAST_MODEL_BY_THREAD.clear()
    _patch_history(monkeypatch, [{"role": "user", "content": "hi"}])
    out = asyncio.run(handle_model_switch("t1", "gpt-4.1-mini", FakeOpenAI(), db=db))
    assert out is None
    assert get_last_model("t1") == "gpt-4.1-mini"


def test_summary_on_provider_switch(db, ollama_registered, monkeypatch):
    from services.providers.chat_session import _LAST_MODEL_BY_THREAD
    _LAST_MODEL_BY_THREAD.clear()
    _patch_history(monkeypatch, [
        {"role": "user", "content": "Help me design a schema"},
        {"role": "assistant", "content": "Sure, here is a star schema"},
    ])
    fake = FakeOpenAI()
    # establish gpt as the current model (openai)
    assert asyncio.run(handle_model_switch("t1", "gpt-4.1-mini", fake, db=db)) is None
    # switch to qwen (ollama) -> provider changed -> OLD model (gpt/openai) summarizes
    summary = asyncio.run(handle_model_switch("t1", "qwen2.5:14b", fake, db=db))
    assert summary == "HANDOFF SUMMARY"


def test_no_summary_on_same_provider_switch(db, monkeypatch):
    from services.providers.chat_session import _LAST_MODEL_BY_THREAD
    _LAST_MODEL_BY_THREAD.clear()
    _patch_history(monkeypatch, [{"role": "user", "content": "x"}])
    fake = FakeOpenAI()
    assert asyncio.run(handle_model_switch("t2", "gpt-4.1-mini", fake, db=db)) is None
    # gpt -> gpt-5 : both openai -> no handoff needed (shared history is enough)
    assert asyncio.run(handle_model_switch("t2", "gpt-5", fake, db=db)) is None


def test_switch_persists_summary_and_seeds_from_prior(db, ollama_registered, monkeypatch):
    """A provider switch persists the handoff summary (ThreadCompaction store) and
    seeds the OLD model with the previously persisted summary, so repeated switches
    build on it instead of re-summarising the whole conversation from scratch."""
    import time
    from services.providers.chat_session import _LAST_MODEL_BY_THREAD, set_last_model
    from services.compaction_service import latest_compaction_summary
    from models.token_usage import ThreadCompaction

    ThreadCompaction.__table__.create(bind=db.get_bind())
    _LAST_MODEL_BY_THREAD.clear()
    _patch_history(monkeypatch, [
        {"role": "user", "content": "newest user turn"},
        {"role": "assistant", "content": "newest assistant turn"},
    ])
    # A summary from an earlier switch already exists for this thread.
    db.add(ThreadCompaction(thread_id="tP", summary="PRIOR SUMMARY TEXT", created_at=time.time()))
    db.commit()

    captured = {}

    class CapturingOpenAI(FakeOpenAI):
        async def ask_async(self, user_input=None, *a, **k):
            captured["prompt"] = user_input
            return type("R", (), {"text": "NEW SUMMARY", "response_id": "1", "raw": None})()

    set_last_model("tP", "gpt-4.1-mini")  # current model is OpenAI (summariser stays off-network)
    summary = asyncio.run(handle_model_switch("tP", "qwen2.5:14b", CapturingOpenAI(), db=db))

    assert summary == "NEW SUMMARY"
    # The OLD model built on the prior summary rather than from scratch.
    assert "PRIOR SUMMARY TEXT" in (captured["prompt"] or "")
    assert "Earlier summary" in (captured["prompt"] or "")
    # The new summary is persisted, so the next switch can reuse it.
    assert latest_compaction_summary(db, "tP") == "NEW SUMMARY"


def test_empty_history_yields_no_summary(db, ollama_registered, monkeypatch):
    from services.providers.chat_session import _LAST_MODEL_BY_THREAD
    _LAST_MODEL_BY_THREAD.clear()
    _patch_history(monkeypatch, [])
    fake = FakeOpenAI()
    asyncio.run(handle_model_switch("t3", "gpt-4.1-mini", fake, db=db))
    assert asyncio.run(handle_model_switch("t3", "qwen2.5:14b", fake, db=db)) is None
