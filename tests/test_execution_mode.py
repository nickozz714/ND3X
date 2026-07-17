"""Agent-mode primitives: slot_mode (empty/model/agent), CAP_CLASS coverage,
and the is_cli_agent capability marker. No call-site behavior is tested here —
Fase 0 adds only the primitives."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from schemas.provider import ProviderCreate, ProviderModelCreate
from services.providers.base import ChatProvider
from services.providers.capability_router import ALL_SLOTS
from services.providers.claude_code_provider import ClaudeCodeChatProvider
from services.providers.execution_mode import (
    CAP_CLASS,
    MODALITY,
    OUTSOURCEABLE,
    capability_class,
    is_cli_agent_type,
    slot_mode,
)
from services.providers.registry_service import ProviderRegistryService


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


def _register(db, *, provider_type="ollama", model_id="qwen2.5:14b", enabled=True):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(
        name=provider_type, provider_type=provider_type,
        base_url="http://localhost:11434", is_local=(provider_type == "ollama"),
        enabled=enabled,
    ))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id=model_id, capability="chat"))
    return reg, p, m


# ── CAP_CLASS ────────────────────────────────────────────────────────────────

def test_cap_class_covers_every_routing_slot_exactly():
    assert set(CAP_CLASS) == set(ALL_SLOTS)


def test_cap_class_outsourceable_vs_modality():
    assert capability_class("chat.planner") == OUTSOURCEABLE
    assert capability_class("chat.cognition") == OUTSOURCEABLE
    assert capability_class("chat.memory_decision") == OUTSOURCEABLE
    assert capability_class("chat.auto_decision") == OUTSOURCEABLE
    for slot in ("embeddings", "transcription", "tts", "voice", "realtime", "image_generation"):
        assert capability_class(slot) == MODALITY
    assert capability_class("no.such.slot") is None
    assert capability_class(None) is None


# ── is_cli_agent capability ──────────────────────────────────────────────────

def test_claude_code_is_a_cli_agent_by_capability():
    assert ClaudeCodeChatProvider.is_cli_agent is True
    assert is_cli_agent_type("claude_code") is True


def test_plain_providers_are_not_cli_agents():
    assert ChatProvider.is_cli_agent is False
    for t in ("ollama", "openai", "openai_compatible", "anthropic", "gemini", "", None, "unknown"):
        assert is_cli_agent_type(t) is False


def test_execution_mode_property_derives_from_capability():
    class _Plain(ChatProvider):
        provider_type = "test_plain"

        async def chat(self, user_input, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

    class _Agent(ChatProvider):
        provider_type = "test_agent"
        is_cli_agent = True

        async def chat(self, user_input, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

    assert _Plain().execution_mode == "model"
    assert _Agent().execution_mode == "agent"
    # subclass definition registered the types on the ChatProvider registry
    assert is_cli_agent_type("test_agent") is True
    assert is_cli_agent_type("test_plain") is False


# ── slot_mode ────────────────────────────────────────────────────────────────

def test_slot_mode_unassigned_is_none(db):
    assert slot_mode(db, "chat.planner") is None
    assert slot_mode(db, "chat.cognition") is None


def test_slot_mode_plain_model(db):
    reg, _p, m = _register(db, provider_type="ollama")
    reg.set_assignment("chat.planner", m.id)
    assert slot_mode(db, "chat.planner") == "model"


def test_slot_mode_cli_agent(db):
    reg, _p, m = _register(db, provider_type="claude_code", model_id="opus")
    reg.set_assignment("chat.cognition", m.id)
    assert slot_mode(db, "chat.cognition") == "agent"
    # other slots stay unassigned → None (no fallback)
    assert slot_mode(db, "chat.planner") is None


def test_slot_mode_disabled_provider_is_none(db):
    reg, p, m = _register(db, provider_type="claude_code", model_id="opus", enabled=False)
    reg.set_assignment("chat.planner", m.id)
    assert slot_mode(db, "chat.planner") is None


# ── Fase 2: modality guard at assignment (no fallback) ───────────────────────

def test_cli_agent_rejected_on_modality_slots(db):
    reg, _p, m = _register(db, provider_type="claude_code", model_id="opus")
    for slot in ("embeddings", "transcription", "tts", "voice", "realtime", "image_generation"):
        with pytest.raises(ValueError, match="modality slot"):
            reg.set_assignment(slot, m.id)


def test_cli_agent_allowed_on_outsourceable_slots(db):
    reg, _p, m = _register(db, provider_type="claude_code", model_id="opus")
    # No exception — an outsourceable slot accepts a CLI-agent (agent mode).
    reg.set_assignment("chat.planner", m.id)
    assert slot_mode(db, "chat.planner") == "agent"


def test_plain_model_still_allowed_on_modality_slots(db):
    # The guard only rejects CLI-agent providers; normal models are unaffected.
    reg, _p, m = _register(db, provider_type="ollama", model_id="nomic-embed")
    reg.set_assignment("embeddings", m.id)  # must not raise


# ── Fase 5: provider-agnostic (Codex-ready) smoke ────────────────────────────

def test_capability_path_is_provider_agnostic():
    """A hypothetical 2nd CLI agent (e.g. Codex) is recognized purely by the
    is_cli_agent capability — no provider_type name check anywhere. Defining the
    ChatProvider subclass auto-registers it on the type registry."""
    from services.providers.base import ChatProvider, ChatResult

    class _CodexProvider(ChatProvider):
        provider_type = "codex_cli_test"
        is_cli_agent = True

        async def chat(self, *a, **k):  # pragma: no cover - not called
            return ChatResult(text="", provider=self.provider_type, model="x")

    assert is_cli_agent_type("codex_cli_test") is True
    assert ProviderRegistryService._execution_mode("codex_cli_test", True) == "agent"
    assert ProviderRegistryService._execution_mode("ollama", True) == "model"
    assert ProviderRegistryService._execution_mode("codex_cli_test", False) is None
