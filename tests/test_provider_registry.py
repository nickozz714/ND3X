"""Unit tests for the provider/model registry (Phase 0).

Uses a real in-memory SQLite DB with the provider tables, and exercises CRUD,
encrypted-key handling, capability assignment, and slot resolution.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.providers.registry_service import ProviderRegistryService
from schemas.provider import (
    ProviderCreate,
    ProviderUpdate,
    ProviderModelCreate,
    ProviderModelUpdate,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (pv.Provider, pv.ProviderModel, pv.CapabilityAssignment):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def svc(db):
    return ProviderRegistryService(db)


def test_create_provider_encrypts_key_and_hides_plaintext(svc):
    read = svc.create_provider(ProviderCreate(
        name="Anthropic", provider_type="anthropic", api_key="sk-ant-secret"))
    assert read.has_api_key is True
    # plaintext is never exposed on the read model
    assert not hasattr(read, "api_key")
    # but the server can decrypt it for outbound calls
    assert svc.get_api_key(read.id) == "sk-ant-secret"
    # and the stored column is not the plaintext
    obj = svc.get_provider(read.id)
    assert obj.api_key_encrypted and obj.api_key_encrypted != "sk-ant-secret"


def test_provider_without_key(svc):
    read = svc.create_provider(ProviderCreate(name="LocalOllama", provider_type="ollama", is_local=True))
    assert read.has_api_key is False
    assert svc.get_api_key(read.id) is None


def test_update_provider_key_rotation_and_clear(svc):
    p = svc.create_provider(ProviderCreate(name="P", provider_type="openai", api_key="k1"))
    # update an unrelated field without touching the key -> key preserved
    svc.update_provider(p.id, ProviderUpdate(base_url="https://x"))
    assert svc.get_api_key(p.id) == "k1"
    # rotate the key
    svc.update_provider(p.id, ProviderUpdate(api_key="k2"))
    assert svc.get_api_key(p.id) == "k2"
    # clear the key
    r = svc.update_provider(p.id, ProviderUpdate(api_key=None))
    assert r.has_api_key is False and svc.get_api_key(p.id) is None


def test_provider_models_crud_and_filter(svc):
    p = svc.create_provider(ProviderCreate(name="P", provider_type="anthropic", api_key="k"))
    m1 = svc.create_model(ProviderModelCreate(provider_id=p.id, model_id="claude-opus-4-8", capability="chat", good_for="reasoning"))
    svc.create_model(ProviderModelCreate(provider_id=p.id, model_id="claude-haiku-4-5", capability="chat"))
    assert len(svc.list_models(provider_id=p.id)) == 2
    assert len(svc.list_models(provider_id=p.id, capability="chat")) == 2
    assert len(svc.list_models(provider_id=p.id, capability="embeddings")) == 0
    upd = svc.update_model(m1.id, ProviderModelUpdate(enabled=False, good_for="updated"))
    assert upd.enabled is False and upd.good_for == "updated"
    assert svc.delete_model(m1.id) is True
    assert len(svc.list_models(provider_id=p.id)) == 1


def test_assignment_and_resolution(svc):
    p = svc.create_provider(ProviderCreate(name="Anthropic", provider_type="anthropic", api_key="k"))
    m = svc.create_model(ProviderModelCreate(provider_id=p.id, model_id="claude-opus-4-8", capability="chat"))

    # unassigned slot -> None
    assert svc.resolve_slot("chat.planner") is None

    svc.set_assignment("chat.planner", m.id)
    resolved = svc.resolve_slot("chat.planner")
    assert resolved is not None
    assert resolved.provider_type == "anthropic"
    assert resolved.model_id == "claude-opus-4-8"
    assert resolved.has_api_key is True

    # disabling the model unresolves the slot
    svc.update_model(m.id, ProviderModelUpdate(enabled=False))
    assert svc.resolve_slot("chat.planner") is None
    svc.update_model(m.id, ProviderModelUpdate(enabled=True))

    # disabling the provider unresolves the slot
    svc.update_provider(p.id, ProviderUpdate(enabled=False))
    assert svc.resolve_slot("chat.planner") is None

    # clearing the assignment -> None
    svc.update_provider(p.id, ProviderUpdate(enabled=True))
    svc.set_assignment("chat.planner", None)
    assert svc.resolve_slot("chat.planner") is None


def test_planner_native_vision_model(svc):
    # Vision-capable model on the planner slot → native multimodal passthrough.
    p = svc.create_provider(ProviderCreate(name="Anthropic", provider_type="anthropic", api_key="k"))
    m = svc.create_model(ProviderModelCreate(provider_id=p.id, model_id="claude-opus-4-8", capability="chat"))
    svc.set_assignment("chat.planner", m.id)
    assert svc.planner_native_vision_model(None) == "claude-opus-4-8"

    # Forced text-only active model → describe fallback (None).
    o = svc.create_provider(ProviderCreate(name="LocalOllama", provider_type="ollama", is_local=True))
    svc.create_model(ProviderModelCreate(provider_id=o.id, model_id="qwen2.5:14b", capability="chat"))
    assert svc.planner_native_vision_model("qwen2.5:14b") is None

    # Forced local vision model → native, fully local.
    svc.create_model(ProviderModelCreate(provider_id=o.id, model_id="qwen2.5vl:7b", capability="chat"))
    assert svc.planner_native_vision_model("qwen2.5vl:7b") == "qwen2.5vl:7b"


def test_planner_native_vision_model_excludes_cli_agents(svc):
    # A CLI-agent planner drives its own loop from the plain conversation —
    # content blocks never reach it, so it must keep the describe path.
    p = svc.create_provider(ProviderCreate(name="CC", provider_type="claude_code"))
    m = svc.create_model(ProviderModelCreate(
        provider_id=p.id, model_id="claude-sonnet-5", capability="chat", supports_vision=True))
    svc.set_assignment("chat.planner", m.id)
    assert svc.planner_native_vision_model(None) is None


def test_delete_provider_cascades_models(svc):
    p = svc.create_provider(ProviderCreate(name="P", provider_type="openai", api_key="k"))
    svc.create_model(ProviderModelCreate(provider_id=p.id, model_id="gpt-x", capability="chat"))
    assert svc.delete_provider(p.id) is True
    assert svc.get_provider(p.id) is None
    assert svc.list_models() == []
