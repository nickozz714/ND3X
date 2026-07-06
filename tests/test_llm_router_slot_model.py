"""LLMRouter uses the slot-resolved model id on the OpenAI base path.

Regression for Theme 1: an OpenAI-backed chat slot was ignored because the base
OpenAI call used whatever model string the caller passed (settings.LLM_MODEL),
not the slot's model.
"""
from __future__ import annotations

import asyncio

from services.providers.llm_router import LLMRouter


class _FakeOpenAI:
    def __init__(self):
        self.calls = []
        self.embed_calls = []
        self.default_model = "base-default"
        self.default_embedding_model = "base-embed-default"

    async def ask_orchestration_async(self, user_input, *, role, model=None, **kwargs):
        self.calls.append({"role": role, "model": model})
        return {"answer": "ok"}

    def embed(self, text, *, model=None, **kwargs):
        self.embed_calls.append({"model": model})
        return [0.0]


def test_openai_base_path_uses_slot_model_id():
    fake = _FakeOpenAI()
    router = LLMRouter(
        fake,
        resolve_chat_provider=lambda model, role: None,           # OpenAI base path
        resolve_chat_model=lambda model, role: "gpt-from-slot" if model is None else model,
        capabilities={"chat": True},
    )
    asyncio.run(router.ask_orchestration_async("hi", role="router:1", model=None))
    assert fake.calls[-1]["model"] == "gpt-from-slot"


def test_default_passthrough_keeps_explicit_model():
    fake = _FakeOpenAI()
    router = LLMRouter(
        fake,
        resolve_chat_provider=lambda model, role: None,
        capabilities={"chat": True},
    )
    asyncio.run(router.ask_orchestration_async("hi", role="router:1", model="explicit"))
    assert fake.calls[-1]["model"] == "explicit"


def test_embed_base_path_uses_slot_model_id():
    fake = _FakeOpenAI()
    router = LLMRouter(
        fake,
        resolve_embedding_provider=lambda model: None,             # OpenAI base path
        resolve_embedding_model=lambda model: "embed-from-slot",
        capabilities={"embeddings": True},
    )
    router.embed("hello", model=None)
    assert fake.embed_calls[-1]["model"] == "embed-from-slot"
