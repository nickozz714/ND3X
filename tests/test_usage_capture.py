"""Token-usage capture: the per-request accumulator + provider usage extraction."""
from __future__ import annotations

from types import SimpleNamespace

from services.providers import usage_accumulator as acc
from services.openai_service import OpenAIResponsesService


def test_accumulator_reset_add_drain():
    acc.reset()
    acc.add(input_tokens=100, output_tokens=20, model="m", provider_type="openai", role="router:1")
    acc.add(input_tokens=200, output_tokens=30, model="m", provider_type="anthropic")
    ev = acc.drain()
    assert [e["input_tokens"] for e in ev] == [100, 200]
    assert ev[0]["provider_type"] == "openai"
    assert ev[1]["provider_type"] == "anthropic"


def test_openai_usage_extraction_responses_fields():
    acc.reset()
    resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=512, output_tokens=64), id="r1")
    OpenAIResponsesService._record_response_usage(resp, model="gpt-5.5-pro", role="writer:doc:1")
    ev = acc.drain()
    assert len(ev) == 1
    assert ev[0]["input_tokens"] == 512 and ev[0]["output_tokens"] == 64
    assert ev[0]["model"] == "gpt-5.5-pro" and ev[0]["provider_type"] == "openai"
    assert ev[0]["role"] == "writer:doc:1"  # stage/role is captured, not dropped


def test_openai_usage_extraction_prompt_completion_fields():
    acc.reset()
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40))
    OpenAIResponsesService._record_response_usage(resp, model="x")
    ev = acc.drain()
    assert ev[0]["input_tokens"] == 300 and ev[0]["output_tokens"] == 40


def test_missing_usage_is_safe():
    acc.reset()
    OpenAIResponsesService._record_response_usage(SimpleNamespace(), model="x")
    assert acc.drain() == []


def test_add_inherits_role_from_contextvar():
    """An add() without an explicit role inherits the stage set via set_role, so
    alternate-provider adapters (which don't know the role) still get tagged."""
    acc.reset()
    tok = acc.set_role("planner:3")
    try:
        acc.add(input_tokens=10, output_tokens=2, provider_type="anthropic")  # no role
    finally:
        acc.reset_role(tok)
    acc.add(input_tokens=5, output_tokens=1, provider_type="anthropic")  # outside set_role
    ev = acc.drain()
    assert ev[0]["role"] == "planner:3"   # inherited from the boundary
    assert ev[1]["role"] is None          # no role set -> stays None
    # an explicit role always wins over the contextvar
    acc.reset()
    tok = acc.set_role("planner:3")
    try:
        acc.add(input_tokens=1, output_tokens=1, role="router:1", provider_type="openai")
    finally:
        acc.reset_role(tok)
    assert acc.drain()[0]["role"] == "router:1"


def test_llm_router_tags_alternate_provider_usage_with_stage():
    """ask_orchestration_async routes to an alternate provider; the provider's
    adapter records usage without a role, but the by-stage tag is preserved."""
    import asyncio
    from services.providers.base import ChatProvider, ChatResult
    from services.providers.llm_router import LLMRouter

    class _UsageProvider(ChatProvider):
        provider_type = "anthropic"
        supports_structured_output = True

        async def chat(self, user_input, *, model=None, **kwargs):
            # Mirror the real adapters: record usage WITHOUT passing a role.
            acc.add(input_tokens=42, output_tokens=7, model=model, provider_type=self.provider_type)
            return ChatResult(text="{}", provider=self.provider_type, model=model or "")

    prov = _UsageProvider()
    router = LLMRouter(SimpleNamespace(default_model=None, default_embedding_model=None),
                       resolve_chat_provider=lambda m, r: (prov, "claude-opus-4-8"))
    acc.reset()
    asyncio.run(router.ask_orchestration_async("hi", role="router:7", json_schema={"type": "object"}))
    ev = acc.drain()
    assert len(ev) == 1
    assert ev[0]["provider_type"] == "anthropic"
    assert ev[0]["role"] == "router:7"   # stage tagged at the boundary, not dropped
