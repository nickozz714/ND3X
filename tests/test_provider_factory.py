"""Unit tests for the provider factory + router builder (Phase 1).

Verifies registry-driven resolution: a registered Claude model routes to the
Anthropic adapter, gpt models fall through to OpenAI, and the embeddings slot
routes to a compatible embedding adapter. No network (adapters are only
constructed, not called).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.providers.anthropic_provider import AnthropicChatProvider
from services.providers.openai_compatible_provider import OpenAICompatibleEmbeddingProvider
from services.providers.provider_factory import (
    _build_chat_provider,
    _build_embedding_provider,
    build_llm_router,
)
from services.providers.registry_service import ProviderRegistryService
from schemas.provider import ProviderCreate, ProviderModelCreate


class _FakeOpenAI:
    default_model = "gpt-4.1-mini"
    default_embedding_model = "text-embedding-3-small"

    async def ask_async(self, *a, **k):
        return type("R", (), {"text": "openai", "response_id": "1", "raw": None})()


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


def test_build_chat_provider_types(db):
    p = pv.Provider(name="A", provider_type="anthropic")
    assert isinstance(_build_chat_provider(p, "key", "claude-opus-4-8", None), AnthropicChatProvider)
    # anthropic without key -> None
    assert _build_chat_provider(pv.Provider(name="B", provider_type="anthropic"), None, "claude-opus-4-8", None) is None
    # anthropic has no embeddings
    assert _build_embedding_provider(p, "key", "x", None) is None
    # ollama embeddings -> compatible adapter
    ol = pv.Provider(name="O", provider_type="ollama", base_url="http://localhost:11434/v1")
    assert isinstance(_build_embedding_provider(ol, None, "nomic-embed", None), OpenAICompatibleEmbeddingProvider)


def test_router_resolves_claude_model_and_passes_gpt_through(db):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Anthropic", provider_type="anthropic", api_key="sk-ant"))
    reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="claude-opus-4-8", capability="chat"))

    router = build_llm_router(_FakeOpenAI(), db)
    # claude model resolves to the Anthropic adapter (returns (provider, model))
    resolved = router._resolve_chat("claude-opus-4-8", None)
    assert resolved is not None and isinstance(resolved[0], AnthropicChatProvider)
    assert resolved[1] == "claude-opus-4-8"
    # Slots are authoritative: a registered claude model that is NOT assigned to a
    # chat slot does NOT get borrowed for an unknown gpt request — resolution
    # returns None (and the LLMRouter enforces the chat-required error upstream).
    assert router._resolve_chat("gpt-4.1-mini", None) is None
    # disabled provider -> registry empty -> still None
    reg.update_provider(p.id, __import__("schemas.provider", fromlist=["ProviderUpdate"]).ProviderUpdate(enabled=False))
    router2 = build_llm_router(_FakeOpenAI(), db)
    assert router2._resolve_chat("claude-opus-4-8", None) is None


def test_role_to_slot_mapping():
    from services.providers.provider_factory import role_to_slot
    assert role_to_slot("router:5") == "chat.router"
    assert role_to_slot("writer:Foo:5") == "chat.final_answer"
    assert role_to_slot("assistant:Foo") == "chat.planner"
    assert role_to_slot("workflow_planner:Foo:5") == "chat.planner"
    assert role_to_slot("cognition:MemoryExtractor") == "chat.cognition"
    assert role_to_slot("memory_decision:router") == "chat.memory_decision"
    assert role_to_slot("memory_decision:planner") == "chat.memory_decision"
    assert role_to_slot(None) == "chat.planner"


def test_cognition_slot_routes_to_assigned_model(db):
    """The cognition role resolves to the chat.cognition slot's model."""
    from services.providers.ollama_provider import OllamaChatProvider
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="qwen2.5:14b", capability="chat"))
    reg.set_assignment("chat.cognition", m.id)

    router = build_llm_router(_FakeOpenAI(), db)
    resolved = router._resolve_chat("gpt-4o-mini", "cognition:MemoryExtractor")
    assert resolved is not None and isinstance(resolved[0], OllamaChatProvider)
    assert resolved[1] == "qwen2.5:14b"


def test_router_resolves_chat_by_role_slot_and_overrides_model(db):
    from services.providers.ollama_provider import OllamaChatProvider
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="qwen2.5:14b", capability="chat"))
    reg.set_assignment("chat.planner", m.id)

    router = build_llm_router(_FakeOpenAI(), db)
    # A planner role with a gpt model -> the slot overrides to the local model.
    resolved = router._resolve_chat("gpt-4.1-mini", "assistant:Foo")
    assert resolved is not None and isinstance(resolved[0], OllamaChatProvider)
    assert resolved[1] == "qwen2.5:14b"   # slot model wins over the requested gpt
    # The router slot has no assignment -> agnostic default (chat.planner = qwen),
    # so even unassigned roles use the registered model instead of OpenAI.
    router_resolved = router._resolve_chat("gpt-4.1-mini", "router:1")
    assert router_resolved is not None and router_resolved[1] == "qwen2.5:14b"


def test_forced_chat_model_overrides_slot(db):
    from services.providers.chat_session import forced_chat_model
    from services.providers.anthropic_provider import AnthropicChatProvider
    reg = ProviderRegistryService(db)
    op = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                            base_url="http://localhost:11434/v1", is_local=True))
    om = reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="qwen2.5:14b", capability="chat"))
    reg.set_assignment("chat.planner", om.id)  # workbench routing -> qwen
    ap = reg.create_provider(ProviderCreate(name="Anthropic", provider_type="anthropic", api_key="k"))
    reg.create_model(ProviderModelCreate(provider_id=ap.id, model_id="claude-opus-4-8", capability="chat"))

    router = build_llm_router(_FakeOpenAI(), db)
    # forced claude overrides the planner slot (qwen)
    tok = forced_chat_model.set("claude-opus-4-8")
    try:
        resolved = router._resolve_chat("gpt-4.1-mini", "assistant:Foo")
        assert resolved is not None and isinstance(resolved[0], AnthropicChatProvider)
        assert resolved[1] == "claude-opus-4-8"
        # forced gpt/default -> OpenAI pass-through even though a slot is set
        forced_chat_model.set("gpt-4.1-mini")
        assert router._resolve_chat("gpt-4.1-mini", "assistant:Foo") is None
    finally:
        forced_chat_model.reset(tok)
    # with no forced model, the slot drives again
    assert router._resolve_chat("gpt-4.1-mini", "assistant:Foo") is not None


def test_forced_model_from_input_payload_honored_for_non_openai(db):
    """The audit `input_payload.forced_model` path flows into `forced_chat_model`
    and is honored for a non-OpenAI (here: openai_compatible/Ollama) provider —
    both the provider AND the effective model id resolve to the forced model."""
    from services.providers.chat_session import forced_chat_model
    from services.providers.ollama_provider import OllamaChatProvider

    reg = ProviderRegistryService(db)
    # Workbench routing points the planner slot at one local model...
    op = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                            base_url="http://localhost:11434/v1", is_local=True))
    slot_m = reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="qwen2.5:14b", capability="chat"))
    reg.set_assignment("chat.planner", slot_m.id)
    # ...but a different local model is registered and will be forced via the payload.
    reg.create_model(ProviderModelCreate(provider_id=op.id, model_id="llama3.1:70b", capability="chat"))

    router = build_llm_router(_FakeOpenAI(), db)

    # Mirror ask_job_callbacks: forced_chat_model is set from input_payload.forced_model.
    input_payload = {"forced_model": "llama3.1:70b"}
    tok = forced_chat_model.set(input_payload.get("forced_model"))
    try:
        resolved = router._resolve_chat("gpt-4.1-mini", "assistant:Foo")
        assert resolved is not None and isinstance(resolved[0], OllamaChatProvider)
        assert resolved[1] == "llama3.1:70b"            # forced model wins over the planner slot
        assert router._resolve_chat_model("gpt-4.1-mini", "assistant:Foo") == "llama3.1:70b"
    finally:
        forced_chat_model.reset(tok)
    # Without a forced model the planner slot drives again.
    assert router._resolve_chat("gpt-4.1-mini", "assistant:Foo")[1] == "qwen2.5:14b"


def test_router_resolves_embeddings_slot(db):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="nomic-embed-text", capability="embeddings"))
    reg.set_assignment("embeddings", m.id)

    router = build_llm_router(_FakeOpenAI(), db)
    emb = router._resolve_embedding(None)
    assert isinstance(emb, OpenAICompatibleEmbeddingProvider)


def test_empty_registry_is_passthrough(db):
    router = build_llm_router(_FakeOpenAI(), db)
    assert router._resolve_chat("anything", None) is None
    assert router._resolve_embedding(None) is None


def test_resolve_embedding_provider_and_default_chat(db):
    from services.providers.provider_factory import resolve_embedding_provider, resolve_default_chat_provider
    from services.providers.openai_provider import OpenAIChatProvider

    reg = ProviderRegistryService(db)
    # no embeddings slot -> None
    assert resolve_embedding_provider(db, _FakeOpenAI()) is None
    # default chat with empty registry -> OpenAI adapter
    assert isinstance(resolve_default_chat_provider(db, _FakeOpenAI()), OpenAIChatProvider)

    # register ollama embeddings + slot
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="nomic-embed-text", capability="embeddings"))
    reg.set_assignment("embeddings", m.id)
    assert isinstance(resolve_embedding_provider(db, _FakeOpenAI()), OpenAICompatibleEmbeddingProvider)

    # a registered claude chat model makes default chat resolve to Anthropic
    ap = reg.create_provider(ProviderCreate(name="Anthropic", provider_type="anthropic", api_key="k"))
    reg.create_model(ProviderModelCreate(provider_id=ap.id, model_id="claude-opus-4-8", capability="chat"))
    assert isinstance(resolve_default_chat_provider(db, _FakeOpenAI()), AnthropicChatProvider)


def test_voice_slot_model_resolvers(db):
    """transcription/tts/voice/realtime slots resolve to their assigned model id;
    an unassigned slot returns the caller fallback (no hardcoded default)."""
    from services.providers.provider_factory import (
        resolve_transcription_model,
        resolve_tts_model,
        resolve_voice_model,
        resolve_realtime_model,
    )
    reg = ProviderRegistryService(db)
    # Unassigned: returns the caller fallback (None by default).
    assert resolve_transcription_model(db) is None
    assert resolve_tts_model(db, "tts-1") == "tts-1"          # explicit fallback passes through
    assert resolve_voice_model(db) is None
    assert resolve_realtime_model(db) is None

    p = reg.create_provider(ProviderCreate(name="OAIcompat", provider_type="openai_compatible",
                                           base_url="http://x/v1", api_key="k"))
    stt = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="whisper-large-v3", capability="transcription"))
    tts = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="kokoro", capability="tts"))
    voice = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="voice-x", capability="voice"))
    rt = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="realtime-x", capability="realtime"))
    reg.set_assignment("transcription", stt.id)
    reg.set_assignment("tts", tts.id)
    reg.set_assignment("voice", voice.id)
    reg.set_assignment("realtime", rt.id)

    # Assigned slot wins over the caller fallback.
    assert resolve_transcription_model(db, "whisper-1") == "whisper-large-v3"
    assert resolve_tts_model(db, "tts-1") == "kokoro"
    assert resolve_voice_model(db) == "voice-x"
    assert resolve_realtime_model(db) == "realtime-x"


def test_routed_embedding_dispatch():
    """RoutedEmbeddingService dispatches to a resolved provider or falls back."""
    from services.providers.routed_embedding import RoutedEmbeddingService

    class _Base:
        def embed(self, t): return [1.0]
        def embed_batch(self, ts): return [[1.0] for _ in ts]
        def cosine_similarity(self, a, b): return 0.5

    class _Prov:
        def embed(self, t, *, model=None): return [9.0]
        def embed_batch(self, ts, *, model=None): return [[9.0] for _ in ts]

    r = RoutedEmbeddingService(_Base())
    # before resolution forced: pretend unresolved -> base
    r._checked = True
    r._resolved = None
    assert r.embed("x") == [1.0]
    assert r.cosine_similarity(1, 2) == 0.5  # passthrough
    # resolved -> provider
    r._resolved = _Prov()
    assert r.embed("x") == [9.0]
    assert r.embed_batch(["a"]) == [[9.0]]
