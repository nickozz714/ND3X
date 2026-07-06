"""Slots are authoritative: required capabilities (chat/embeddings) error when
unassigned; optional ones are reported disabled."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.providers.capability_router import (
    ALL_SLOTS,
    CapabilityNotConfigured,
    compute_capabilities,
    resolved_models,
)
from services.providers.llm_router import LLMRouter
from services.providers.provider_factory import build_llm_router
from services.providers.registry_service import ProviderRegistryService
from schemas.provider import ProviderCreate, ProviderModelCreate


class _FakeOpenAI:
    default_model = "gpt-4.1-mini"
    default_embedding_model = "text-embedding-3-small"

    async def ask_orchestration_async(self, *a, **k):
        return type("R", (), {"text": "ok", "response_id": "1", "raw": None})()

    async def ask_async(self, *a, **k):
        return type("R", (), {"text": "ok", "response_id": "1", "raw": None})()

    def embed(self, *a, **k):
        return [0.1]


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


def _register(db, *, capability="chat", model_id="qwen2.5:14b"):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    return reg, reg.create_model(ProviderModelCreate(provider_id=p.id, model_id=model_id, capability=capability))


def test_compute_capabilities_reflects_assignments(db):
    reg, m = _register(db)
    assert compute_capabilities(db)["chat"] is False
    reg.set_assignment("chat.planner", m.id)
    caps = compute_capabilities(db)
    assert caps["chat"] is True
    assert caps["cognition"] is False and caps["embeddings"] is False


def test_resolved_models_maps_every_slot(db):
    reg, m = _register(db, model_id="qwen2.5:14b")
    reg.set_assignment("chat.planner", m.id)
    resolved = resolved_models(db)
    # Every known slot is present; only the assigned one has a model string.
    assert set(resolved.keys()) == set(ALL_SLOTS)
    assert resolved["chat.planner"] == "ollama:qwen2.5:14b"
    assert resolved["chat.cognition"] is None
    assert resolved["embeddings"] is None


def test_chat_errors_when_no_slot_assigned(db):
    router = build_llm_router(_FakeOpenAI(), db)  # nothing assigned
    with pytest.raises(CapabilityNotConfigured):
        asyncio.run(router.ask_orchestration_async("hi", role="assistant:Foo", model="gpt-4.1-mini"))


def test_chat_works_when_slot_assigned(db):
    reg, m = _register(db)
    reg.set_assignment("chat.planner", m.id)
    router = build_llm_router(_FakeOpenAI(), db)
    # resolves to the assigned local model (no network call, no error)
    resolved = router._resolve_chat("gpt-4.1-mini", "assistant:Foo")
    assert resolved is not None and resolved[1] == "qwen2.5:14b"


def test_forced_model_bypasses_chat_requirement(db):
    from services.providers.chat_session import forced_chat_model
    router = build_llm_router(_FakeOpenAI(), db)  # no slots
    tok = forced_chat_model.set("gpt-4.1-mini")  # explicit user choice -> OpenAI base
    try:
        res = asyncio.run(router.ask_orchestration_async("hi", role="assistant:Foo", model="gpt-4.1-mini"))
        assert res.text == "ok"  # served by base, no CapabilityNotConfigured
    finally:
        forced_chat_model.reset(tok)


def test_embeddings_errors_when_unassigned(db):
    router = build_llm_router(_FakeOpenAI(), db)
    with pytest.raises(CapabilityNotConfigured):
        router.embed("hello")


def test_embeddings_ok_when_assigned(db):
    reg, m = _register(db, capability="embeddings", model_id="nomic-embed-text")
    reg.set_assignment("embeddings", m.id)
    router = build_llm_router(_FakeOpenAI(), db)
    # routed to the assigned (non-OpenAI) embedding provider; no error
    assert router._resolve_embedding(None) is not None


def test_facade_without_capabilities_does_not_enforce():
    # Directly-constructed router (no capabilities) keeps legacy pass-through.
    router = LLMRouter(_FakeOpenAI())
    assert router.embed("x") == [0.1]
