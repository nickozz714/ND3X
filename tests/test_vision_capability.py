"""Vision capability (TODO 5) — curated defaults, per-model override, and the
vision-model resolution used by the attachment describe step + image__view."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from db.database import Base
from schemas.provider import ProviderCreate, ProviderModelCreate, ProviderModelUpdate
from services.providers.registry_service import ProviderRegistryService
from services.providers.vision_capability import effective_vision, provider_supports_vision


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        pv.Provider.__table__, pv.ProviderModel.__table__, pv.CapabilityAssignment.__table__,
    ])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_curated_defaults():
    assert provider_supports_vision("openai", "gpt-5.5") is True
    assert provider_supports_vision("openai", "text-embedding-3-small") is False
    assert provider_supports_vision("anthropic", "claude-opus-4-8") is True
    assert provider_supports_vision("gemini", "gemini-2.0-flash") is True
    assert provider_supports_vision("ollama", "qwen2.5:14b") is False
    assert provider_supports_vision("ollama", "llava:13b") is True
    assert provider_supports_vision("ollama", "llama3.2-vision:11b") is True
    assert provider_supports_vision("ollama", "gemma3:12b") is True


def test_override_wins():
    assert effective_vision("ollama", "qwen2.5:14b", True) is True
    assert effective_vision("openai", "gpt-5.5", False) is False


def test_resolve_vision_model_prefers_active_then_any(db):
    reg = ProviderRegistryService(db)
    op = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                            base_url="http://localhost:11434/v1", is_local=True))
    reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="qwen2.5:14b", capability="chat"))
    lv = reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="llava:13b", capability="chat"))

    # active model is text-only → falls through to the enabled vision model
    assert reg.resolve_vision_model("qwen2.5:14b") == "llava:13b"
    # active model IS vision-capable → used directly
    assert reg.resolve_vision_model("llava:13b") == "llava:13b"

    # nothing vision-capable → None
    reg.update_model(lv.id, ProviderModelUpdate(enabled=False))
    assert reg.resolve_vision_model("qwen2.5:14b") is None


def test_resolve_vision_model_respects_override(db):
    reg = ProviderRegistryService(db)
    op = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                            base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="mystery-model", capability="chat"))
    assert reg.resolve_vision_model() is None
    reg.update_model(m.id, ProviderModelUpdate(supports_vision=True))
    assert reg.resolve_vision_model() == "mystery-model"
