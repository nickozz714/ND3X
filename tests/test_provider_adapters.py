"""Unit tests for the Anthropic and OpenAI-compatible adapters (Phase 1).

Adapters are exercised with injected fake SDK clients — no network.
"""
from __future__ import annotations

import asyncio

from services.providers.anthropic_provider import AnthropicChatProvider
from services.providers.openai_compatible_provider import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)


# ── Anthropic ─────────────────────────────────────────────────────────────────
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 11
    output_tokens = 22


class _AntResp:
    def __init__(self, text="{\"action\": \"final\"}", stop_reason="end_turn"):
        self.id = "msg_1"
        self.content = [_Block("ignored-thinking" if False else text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _FakeMessages:
    def __init__(self, resp):
        self.resp = resp
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.resp


class _FakeAnthropic:
    def __init__(self, resp):
        self.messages = _FakeMessages(resp)


def test_anthropic_chat_builds_request_and_extracts_text():
    fake = _FakeAnthropic(_AntResp())
    p = AnthropicChatProvider(api_key="x", default_model="claude-opus-4-8", client=fake)
    res = asyncio.run(p.chat("plan it", instructions="You are a planner.",
                             response_format={"type": "json_schema"}, max_output_tokens=4096))
    kw = fake.messages.last_kwargs
    assert kw["model"] == "claude-opus-4-8"
    assert kw["max_tokens"] == 4096
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["messages"] == [{"role": "user", "content": "plan it"}]
    assert "You are a planner." in kw["system"]
    assert "JSON" in kw["system"]                     # response_format nudge added
    assert "temperature" not in kw and "top_p" not in kw  # never sent to Claude
    assert res.text == '{"action": "final"}'
    assert res.provider == "anthropic" and res.response_id == "msg_1"
    assert res.usage["input_tokens"] == 11


def test_anthropic_prompt_caching_marks_breakpoints():
    fake = _FakeAnthropic(_AntResp())
    p = AnthropicChatProvider(api_key="x", client=fake, enable_prompt_caching=True)
    asyncio.run(p.chat("plan it", instructions="You are a planner."))
    kw = fake.messages.last_kwargs
    # system + last message become cache-controlled blocks so the prefix is cached.
    assert isinstance(kw["system"], list)
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "You are a planner." in kw["system"][0]["text"]
    last = kw["messages"][-1]
    assert isinstance(last["content"], list)
    assert last["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert last["content"][0]["text"] == "plan it"


def test_anthropic_no_caching_keeps_plain_strings():
    fake = _FakeAnthropic(_AntResp())
    p = AnthropicChatProvider(api_key="x", client=fake)  # caching off (default)
    asyncio.run(p.chat("plan it", instructions="You are a planner."))
    kw = fake.messages.last_kwargs
    assert isinstance(kw["system"], str)
    assert kw["messages"] == [{"role": "user", "content": "plan it"}]


def test_anthropic_refusal_yields_empty_text():
    fake = _FakeAnthropic(_AntResp(text="should be dropped", stop_reason="refusal"))
    p = AnthropicChatProvider(api_key="x", default_model="claude-opus-4-8", client=fake)
    res = asyncio.run(p.chat("hi"))
    assert res.text == ""
    assert res.usage["stop_reason"] == "refusal"


def test_anthropic_message_list_folds_system():
    fake = _FakeAnthropic(_AntResp())
    p = AnthropicChatProvider(api_key="x", client=fake)
    asyncio.run(p.chat([
        {"role": "system", "content": "S1"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
    ], instructions="S0"))
    kw = fake.messages.last_kwargs
    assert kw["system"] == "S0\n\nS1"
    assert kw["messages"] == [{"role": "user", "content": "U1"}, {"role": "assistant", "content": "A1"}]


# ── OpenAI-compatible ─────────────────────────────────────────────────────────
class _ChoiceMsg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _ChoiceMsg(content)


class _CompUsage:
    prompt_tokens = 5
    completion_tokens = 7


class _CompResp:
    def __init__(self, content):
        self.id = "c1"
        self.choices = [_Choice(content)]
        self.usage = _CompUsage()


class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _CompResp(self.content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeCompatClient:
    def __init__(self, content):
        self.chat = _FakeChat(content)


def test_compatible_chat_maps_messages_and_json_format():
    client = _FakeCompatClient("local-answer")
    p = OpenAICompatibleChatProvider(base_url="http://localhost:11434/v1", default_model="qwen2.5", client=client)
    res = asyncio.run(p.chat("hello", instructions="sys", response_format={"x": 1},
                             temperature=0.2, max_output_tokens=512))
    kw = client.chat.completions.last_kwargs
    assert kw["model"] == "qwen2.5"
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert kw["messages"][1] == {"role": "user", "content": "hello"}
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["temperature"] == 0.2 and kw["max_tokens"] == 512
    assert res.text == "local-answer" and res.provider == "openai_compatible"


# ── OpenAI-compatible embeddings ──────────────────────────────────────────────
class _EmbItem:
    def __init__(self, vec):
        self.embedding = vec


class _EmbResp:
    def __init__(self, vecs):
        self.data = [_EmbItem(v) for v in vecs]


class _FakeEmbeddings:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        n = 1 if isinstance(kwargs["input"], str) else len(kwargs["input"])
        return _EmbResp([[0.1, 0.2] for _ in range(n)])


class _FakeEmbClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


def test_compatible_embeddings():
    client = _FakeEmbClient()
    p = OpenAICompatibleEmbeddingProvider(base_url="http://localhost:11434/v1", default_model="nomic-embed", client=client)
    assert p.embed("hi") == [0.1, 0.2]
    assert client.embeddings.last_kwargs["model"] == "nomic-embed"
    out = p.embed_batch(["a", "b"])
    assert out == [[0.1, 0.2], [0.1, 0.2]]
