"""Unit tests for the LLMRouter facade and OpenAI adapter (Phase 0).

Verifies transparent pass-through to the wrapped OpenAI service when no alternate
provider is resolved, dispatch to an alternate provider when one is, and that the
OpenAI adapter wraps results into a normalized ChatResult.
"""
from __future__ import annotations

import asyncio

from services.providers.base import ChatProvider, ChatResult, EmbeddingProvider
from services.providers.llm_router import LLMRouter
from services.providers.openai_provider import OpenAIChatProvider


class FakeResponseResult:
    def __init__(self, text):
        self.text = text
        self.response_id = "resp_1"
        self.raw = {"raw": True}


class FakeOpenAI:
    """Stands in for OpenAIResponsesService."""
    default_model = "gpt-4.1-mini"
    default_embedding_model = "text-embedding-3-small"

    def __init__(self):
        self.calls = []

    async def ask_orchestration_async(self, user_input, *, role, model=None, **kwargs):
        self.calls.append(("ask_orchestration_async", role, model))
        return FakeResponseResult(f"orch:{user_input}")

    async def ask_async(self, user_input, *, model=None, **kwargs):
        self.calls.append(("ask_async", model))
        return FakeResponseResult(f"async:{user_input}")

    def ask(self, user_input, *, model=None, **kwargs):
        self.calls.append(("ask", model))
        return FakeResponseResult(f"sync:{user_input}")

    def embed(self, text, *, model=None, **kwargs):
        self.calls.append(("embed", model))
        return [0.1, 0.2]

    def embed_batch(self, texts, *, model=None, **kwargs):
        self.calls.append(("embed_batch", model))
        return [[0.1], [0.2]]

    def cosine_similarity(self, a, b):
        self.calls.append(("cosine_similarity",))
        return 0.99

    def some_other_method(self):
        return "passthrough"


class RecordingChatProvider(ChatProvider):
    provider_type = "anthropic"

    def __init__(self):
        self.calls = []

    async def chat(self, user_input, *, model=None, **kwargs):
        self.calls.append((user_input, model, kwargs.get("response_format")))
        return ChatResult(text=f"claude:{user_input}", response_id="c1", provider="anthropic", model=model or "")


class StreamingChatProvider(ChatProvider):
    provider_type = "anthropic"
    supports_streaming = True

    async def chat(self, user_input, *, model=None, **kwargs):
        return ChatResult(text="full", provider="anthropic", model=model or "")

    async def chat_stream(self, user_input, *, model=None, **kwargs):
        for piece in ["Hel", "lo ", "wereld"]:
            yield piece


def test_stream_dispatches_to_alternate_provider():
    fake = FakeOpenAI()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (StreamingChatProvider(), "claude-x"))

    async def collect():
        return [d async for d in router.ask_orchestration_stream("hi", role="writer:Agent:1")]

    assert "".join(asyncio.run(collect())) == "Hello wereld"


def test_stream_bridges_openai_sync_iterator():
    fake = FakeOpenAI()
    fake.ask_stream = lambda user_input, **kw: iter(["a", "b", "c"])  # sync iterator
    router = LLMRouter(fake)  # no alternate → OpenAI base path

    async def collect():
        return [d async for d in router.ask_orchestration_stream("hi", role="writer:Agent:1")]

    assert "".join(asyncio.run(collect())) == "abc"


def test_stream_raises_when_alternate_provider_cannot_stream():
    fake = FakeOpenAI()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (RecordingChatProvider(), "claude-x"))

    async def collect():
        return [d async for d in router.ask_orchestration_stream("hi", role="writer:Agent:1")]

    import pytest
    with pytest.raises(NotImplementedError):
        asyncio.run(collect())


class RecordingEmbeddingProvider(EmbeddingProvider):
    provider_type = "ollama"

    def embed(self, text, *, model=None):
        return [9.9]

    def embed_batch(self, texts, *, model=None):
        return [[9.9] for _ in texts]


# ── pass-through (no alternate providers) ─────────────────────────────────────

def test_passthrough_chat_and_embeddings():
    fake = FakeOpenAI()
    router = LLMRouter(fake)
    r = asyncio.run(router.ask_orchestration_async("hi", role="planner", model="gpt-4.1-mini"))
    assert r.text == "orch:hi"
    r2 = asyncio.run(router.ask_async("yo", model=None))
    assert r2.text == "async:yo"
    assert router.embed("x") == [0.1, 0.2]
    assert router.embed_batch(["a", "b"]) == [[0.1], [0.2]]
    assert ("ask_orchestration_async", "planner", "gpt-4.1-mini") in fake.calls
    assert router.default_model == "gpt-4.1-mini"


def test_getattr_passthrough():
    fake = FakeOpenAI()
    router = LLMRouter(fake)
    assert router.cosine_similarity([1], [1]) == 0.99
    assert router.some_other_method() == "passthrough"  # arbitrary attr passthrough


def test_supports_server_side_session_gates_on_resolved_provider():
    """§6: only the OpenAI Responses base path (resolver → None) has a server-side
    session. When a slot pins an alternate provider, the loop is stateless and the
    caller must keep dumping the full accumulated context in the prompt."""
    fake = FakeOpenAI()
    # No alternate resolver → OpenAI Responses base path → has a session.
    assert LLMRouter(fake).supports_server_side_session("gpt-5.5", "assistant:Agent") is True
    # Alternate provider pinned for this role/model → stateless → no session.
    alt = RecordingChatProvider()
    routed = LLMRouter(fake, resolve_chat_provider=lambda model, role: (alt, "qwen2.5:14b"))
    assert routed.supports_server_side_session("qwen2.5:14b", "assistant:Agent") is False


def test_server_side_session_toggle_off_forces_transcript_even_for_openai():
    """AI Models toggle off → even OpenAI runs via the replayed transcript (provider-equal)."""
    fake = FakeOpenAI()
    off = LLMRouter(fake, server_side_session=False)
    assert off.supports_server_side_session("gpt-5.5", "assistant:Agent") is False
    on = LLMRouter(fake, server_side_session=True)
    assert on.supports_server_side_session("gpt-5.5", "assistant:Agent") is True


# ── dispatch to alternate providers ───────────────────────────────────────────

def test_chat_dispatch_to_alternate_provider():
    fake = FakeOpenAI()
    alt = RecordingChatProvider()
    # resolve claude-* to the alternate provider, everything else to OpenAI
    router = LLMRouter(
        fake,
        resolve_chat_provider=lambda model, role: (alt, model) if (model or "").startswith("claude") else None,
    )
    r = asyncio.run(router.ask_orchestration_async("plan it", role="planner",
                                                   model="claude-opus-4-8",
                                                   response_format={"type": "json_schema"}))
    assert r.text == "claude:plan it"
    assert alt.calls and alt.calls[0][1] == "claude-opus-4-8"
    assert alt.calls[0][2] == {"type": "json_schema"}  # response_format forwarded
    # OpenAI was NOT called for the claude model
    assert not any(c[0] == "ask_orchestration_async" for c in fake.calls)

    # a non-claude model still goes to OpenAI
    asyncio.run(router.ask_async("hi", model="gpt-4.1-mini"))
    assert ("ask_async", "gpt-4.1-mini") in fake.calls


def test_embedding_dispatch_to_alternate_provider():
    fake = FakeOpenAI()
    emb = RecordingEmbeddingProvider()
    router = LLMRouter(
        fake,
        resolve_embedding_provider=lambda model: emb if model == "nomic-embed" else None,
    )
    assert router.embed("x", model="nomic-embed") == [9.9]
    assert router.embed("y") == [0.1, 0.2]  # default -> OpenAI


# ── OpenAI adapter ────────────────────────────────────────────────────────────

def test_openai_chat_adapter_wraps_result():
    fake = FakeOpenAI()
    provider = OpenAIChatProvider(fake)
    res = asyncio.run(provider.chat("hello", model="gpt-4.1-mini", response_format={"x": 1}))
    assert isinstance(res, ChatResult)
    assert res.text == "async:hello"
    assert res.provider == "openai"
    assert res.model == "gpt-4.1-mini"
    assert res.response_id == "resp_1"


# ── JSON mode forced for local/structured providers on JSON-producing roles ────

class StructuredChatProvider(ChatProvider):
    provider_type = "openai_compatible"
    supports_structured_output = True

    def __init__(self):
        self.calls = []

    async def chat(self, user_input, *, model=None, **kwargs):
        self.calls.append((kwargs.get("response_format"),))
        return ChatResult(text="{}", response_id="x", provider=self.provider_type, model=model or "")


def test_json_mode_forced_for_router_and_planner_not_writer():
    fake = FakeOpenAI()
    prov = StructuredChatProvider()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (prov, "qwen2.5:14b"))

    # router role -> JSON mode forced
    asyncio.run(router.ask_orchestration_async("q", role="router:1", model="x"))
    assert prov.calls[-1][0] == {"type": "json_object"}
    # planner role (assistant:) -> JSON mode forced
    asyncio.run(router.ask_orchestration_async("q", role="assistant:Foo", model="x"))
    assert prov.calls[-1][0] == {"type": "json_object"}
    # writer (final answer) -> prose, NOT forced
    asyncio.run(router.ask_orchestration_async("q", role="writer:Foo:1", model="x"))
    assert prov.calls[-1][0] is None
    # explicit response_format is respected, not overwritten
    asyncio.run(router.ask_orchestration_async("q", role="router:1", model="x",
                                               response_format={"type": "json_schema"}))
    assert prov.calls[-1][0] == {"type": "json_schema"}


def test_json_mode_not_forced_for_non_structured_provider():
    fake = FakeOpenAI()

    class _Unstructured(RecordingChatProvider):
        supports_structured_output = False

    alt = _Unstructured()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (alt, model))
    asyncio.run(router.ask_orchestration_async("q", role="router:1", model="m"))
    assert alt.calls[-1][2] is None  # response_format not injected when unsupported


# ── sync ask routes to alternate providers (cognition path) ───────────────────

def test_sync_ask_dispatches_to_alternate_provider_with_json_mode():
    fake = FakeOpenAI()
    prov = StructuredChatProvider()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (prov, "qwen2.5:14b"))
    # cognition role (non-writer) -> alternate provider + JSON mode, no OpenAI call
    res = router.ask("extract memories", model="gpt-4o-mini", role="cognition:Mem")
    assert res.model == "qwen2.5:14b"
    assert prov.calls[-1][0] == {"type": "json_object"}
    assert not any(c[0] == "ask" for c in fake.calls)


def test_sync_ask_passthrough_when_unresolved():
    fake = FakeOpenAI()
    router = LLMRouter(fake)  # no alternate resolver
    res = router.ask("hi", model="gpt-4.1-mini")
    assert res.text == "sync:hi"
    assert ("ask", "gpt-4.1-mini") in fake.calls


# ── schema-enforced structured output for local models ────────────────────────

def test_json_schema_threaded_to_structured_provider():
    fake = FakeOpenAI()
    prov = StructuredChatProvider()
    router = LLMRouter(fake, resolve_chat_provider=lambda model, role: (prov, "qwen2.5:14b"))
    schema = {"type": "object", "properties": {"mode": {"type": "string"}, "steps": {"type": "array"}}}
    asyncio.run(router.ask_orchestration_async("route it", role="router:1", model="x", json_schema=schema))
    rf = prov.calls[-1][0]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == schema


def test_json_schema_not_leaked_to_openai_base():
    fake = FakeOpenAI()
    router = LLMRouter(fake)  # no alternate resolver -> base path
    schema = {"type": "object"}
    # must not raise (json_schema is popped before reaching the base service)
    r = asyncio.run(router.ask_orchestration_async("hi", role="router:1", model="gpt-4.1-mini", json_schema=schema))
    assert r.text == "orch:hi"


def test_compatible_provider_passes_json_schema_through():
    from services.providers.openai_compatible_provider import OpenAICompatibleChatProvider

    captured = {}

    class _Resp:
        id = "x"
        choices = [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]
        usage = None

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    captured.update(kw)
                    return _Resp()

    prov = OpenAICompatibleChatProvider(base_url="http://x/v1", default_model="qwen", client=_Client())
    schema_rf = {"type": "json_schema", "json_schema": {"name": "o", "schema": {"type": "object"}}}
    asyncio.run(prov.chat("hi", model="qwen", response_format=schema_rf))
    assert captured["response_format"] == schema_rf  # passed through, not downgraded
    # a non-schema response_format downgrades to json_object
    asyncio.run(prov.chat("hi", model="qwen", response_format={"type": "json_object"}))
    assert captured["response_format"] == {"type": "json_object"}
