"""OpenAIResponsesService resolves its API key lazily from a provider (the
registry's OpenAI provider) — only when a client is first built, not at
construction. There is no global OPEN_AI_API_KEY."""
from __future__ import annotations

from services.openai_service import OpenAIResponsesService


def test_api_key_provider_is_called_lazily_and_cached():
    calls = []

    def provider():
        calls.append(1)
        return "sk-test"

    svc = OpenAIResponsesService(api_key_provider=provider)
    assert calls == []  # not resolved at construction

    client = svc.client  # first access builds the client and resolves the key
    assert calls == [1]
    assert svc.client is client  # cached; provider not called again
    assert calls == [1]


def test_injected_client_is_used_without_resolving_key():
    sentinel = object()
    called = []

    def provider():
        called.append(1)
        return "sk-test"

    svc = OpenAIResponsesService(client=sentinel, api_key_provider=provider)
    assert svc.client is sentinel
    assert called == []  # injected client short-circuits key resolution
