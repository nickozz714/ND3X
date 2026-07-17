"""Azure AI Foundry provider (v1 OpenAI-compatible route) — base-url
normalization, chat/embedding adapters, factory + type-registry registration,
preset, vision/web-search capability defaults, model discovery and health.
No network (fake clients / monkeypatched httpx)."""
from __future__ import annotations

import asyncio

import httpx

import models.provider as pv
from services.providers.azure_foundry_provider import (
    AzureFoundryChatProvider,
    AzureFoundryEmbeddingProvider,
    normalize_foundry_base_url,
)
from services.providers.base import ChatProvider
from services.providers.health_service import check_provider
from services.providers.provider_factory import _build_chat_provider, _build_embedding_provider
from services.providers.provider_presets import PRESETS
from services.providers.vision_capability import provider_supports_vision
from services.providers.web_search_capability import (
    effective_web_search,
    provider_supports_web_search,
)


# ── base-url normalization ────────────────────────────────────────────────────
def test_normalize_foundry_base_url():
    f = normalize_foundry_base_url
    # bare resource endpoints (what users paste from the portal) get the v1 route
    assert f("https://res.openai.azure.com") == "https://res.openai.azure.com/openai/v1"
    assert f("https://res.openai.azure.com/") == "https://res.openai.azure.com/openai/v1"
    assert f("https://res.services.ai.azure.com") == "https://res.services.ai.azure.com/openai/v1"
    assert f("https://res.openai.azure.com/openai") == "https://res.openai.azure.com/openai/v1"
    # already correct → idempotent (trailing slash stripped)
    assert f("https://res.openai.azure.com/openai/v1") == "https://res.openai.azure.com/openai/v1"
    assert f("https://res.openai.azure.com/openai/v1/") == "https://res.openai.azure.com/openai/v1"
    # custom gateway/APIM paths stay untouched
    assert f("https://gw.example.com/foundry/openai/v1") == "https://gw.example.com/foundry/openai/v1"
    assert f("https://gw.example.com/custom-path") == "https://gw.example.com/custom-path"
    assert f(None) is None


# ── chat adapter (fake client) ────────────────────────────────────────────────
class _Msg:
    content = "hallo vanuit foundry"


class _Choice:
    message = _Msg()


class _Usage:
    prompt_tokens = 7
    completion_tokens = 3


class _Resp:
    id = "resp-1"
    choices = [_Choice()]
    usage = _Usage()


class _FakeCompletions:
    def __init__(self):
        self.last_req = None

    async def create(self, **req):
        self.last_req = req
        return _Resp()


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeAsyncClient:
    def __init__(self):
        self.chat = _FakeChat()


def test_chat_adapter_reports_type_and_uses_deployment_name():
    client = _FakeAsyncClient()
    p = AzureFoundryChatProvider(base_url="https://res.openai.azure.com",
                                 default_model="gpt-4o-deploy", client=client)
    r = asyncio.run(p.chat("hoi", instructions="wees kort"))
    assert r.provider == "azure_foundry"
    assert r.text == "hallo vanuit foundry"
    req = client.chat.completions.last_req
    assert req["model"] == "gpt-4o-deploy"  # model = DEPLOYMENT name
    assert req["messages"][0] == {"role": "system", "content": "wees kort"}


def test_chat_adapter_builds_normalized_cloud_client():
    # No fake client → the adapter builds a real (unused) SDK client; verify the
    # normalized base_url and cloud retry policy without any network call.
    p = AzureFoundryChatProvider(base_url="https://res.openai.azure.com", api_key="k")
    assert str(p._client.base_url).rstrip("/").endswith("res.openai.azure.com/openai/v1")
    assert p._client.max_retries == 2


# ── embedding adapter (fake client) ───────────────────────────────────────────
class _EmbItem:
    def __init__(self, vec):
        self.embedding = vec


class _EmbResp:
    def __init__(self, n):
        self.data = [_EmbItem([0.1, 0.2]) for _ in range(n)]


class _FakeEmbeddings:
    def __init__(self):
        self.last_req = None

    def create(self, **req):
        self.last_req = req
        inp = req.get("input")
        return _EmbResp(len(inp) if isinstance(inp, list) else 1)


class _FakeSyncClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


def test_embedding_adapter():
    client = _FakeSyncClient()
    p = AzureFoundryEmbeddingProvider(base_url="https://res.openai.azure.com",
                                      default_model="text-embedding-3-large", client=client)
    vec = p.embed("tekst")
    assert vec == [0.1, 0.2]
    assert client.embeddings.last_req["model"] == "text-embedding-3-large"
    assert len(p.embed_batch(["a", "b"])) == 2


# ── registration: factory, type registry, PROVIDER_TYPES, preset ─────────────
def test_factory_builds_chat_provider_and_requires_key():
    p = pv.Provider(name="F", provider_type="azure_foundry",
                    base_url="https://res.openai.azure.com")
    prov = _build_chat_provider(p, "key", "gpt-4o-deploy", None)
    assert isinstance(prov, AzureFoundryChatProvider)
    # phase-1 auth is API key: missing key = config error, no adapter
    assert _build_chat_provider(p, None, "gpt-4o-deploy", None) is None


def test_factory_builds_embedding_provider_and_requires_key():
    p = pv.Provider(name="F", provider_type="azure_foundry",
                    base_url="https://res.openai.azure.com")
    prov = _build_embedding_provider(p, "key", "text-embedding-3-large", None)
    assert isinstance(prov, AzureFoundryEmbeddingProvider)
    assert _build_embedding_provider(p, None, "text-embedding-3-large", None) is None


def test_registered_type_and_preset():
    assert "azure_foundry" in pv.PROVIDER_TYPES
    assert ChatProvider.class_for_type("azure_foundry") is AzureFoundryChatProvider
    assert AzureFoundryChatProvider.is_cli_agent is False  # plain model provider
    preset = next(p for p in PRESETS if p["key"] == "azure_foundry")
    assert preset["provider_type"] == "azure_foundry"
    assert preset["needs_base_url"] is True  # per-resource endpoint
    assert preset["capabilities"] == ["chat", "embeddings"]


# ── capability defaults ───────────────────────────────────────────────────────
def test_vision_capability_covers_openai_and_open_models():
    assert provider_supports_vision("azure_foundry", "gpt-4o-deploy") is True
    assert provider_supports_vision("azure_foundry", "llama-3.2-vision-deploy") is True
    assert provider_supports_vision("azure_foundry", "phi-4-multimodal-instruct") is True
    assert provider_supports_vision("azure_foundry", "text-embedding-3-large") is False
    assert provider_supports_vision("azure_foundry", "deepseek-v3") is False


def test_web_search_default_off_override_wins():
    assert provider_supports_web_search("azure_foundry", "gpt-4o-deploy") is False
    assert effective_web_search("azure_foundry", "gpt-4o-deploy", True) is True


# ── model discovery (deployments first, v1 catalog as fallback) ──────────────
def test_discovery_prefers_deployments(monkeypatch):
    from services.providers import model_catalog as mc
    from services.providers.model_discovery import discover_models
    monkeypatch.setattr(mc, "fetch_catalog", lambda **kw: {})

    captured = {}

    def fake_get(url, timeout=None, headers=None, params=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["params"] = params or {}
        # ids = DEPLOYMENT names (what ND3X registers as model_id)
        return httpx.Response(
            200, json={"data": [{"id": "gpt-4o-deploy", "model": "gpt-4o"},
                                {"id": "text-embedding-3-large", "model": "text-embedding-3-large"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    out = discover_models(provider_type="azure_foundry",
                          base_url="https://res.openai.azure.com", api_key="k")
    assert captured["url"] == "https://res.openai.azure.com/openai/deployments"
    assert captured["params"]["api-version"] == "2023-03-15-preview"
    # the endpoint accepts both auth headers — discovery sends both
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["headers"]["api-key"] == "k"
    caps = {m["model_id"]: m["capability"] for m in out["models"]}
    assert caps["gpt-4o-deploy"] == "chat"
    assert caps["text-embedding-3-large"] == "embeddings"


def test_discovery_falls_back_to_v1_catalog(monkeypatch):
    from services.providers import model_catalog as mc
    from services.providers.model_discovery import discover_models
    monkeypatch.setattr(mc, "fetch_catalog", lambda **kw: {})

    urls = []

    def fake_get(url, timeout=None, headers=None, params=None):
        urls.append(url)
        if url.endswith("/openai/deployments"):  # legacy route unavailable
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"data": [{"id": "DeepSeek-V3.2"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    out = discover_models(provider_type="azure_foundry",
                          base_url="https://res.openai.azure.com", api_key="k")
    assert urls == ["https://res.openai.azure.com/openai/deployments",
                    "https://res.openai.azure.com/openai/v1/models"]
    assert [m["model_id"] for m in out["models"]] == ["DeepSeek-V3.2"]


def test_discovery_requires_key_and_base_url():
    from services.providers.model_discovery import discover_models
    assert discover_models(provider_type="azure_foundry",
                           base_url="https://res.openai.azure.com", api_key=None)["error"]
    assert discover_models(provider_type="azure_foundry", base_url=None, api_key="k")["error"]


# ── health ────────────────────────────────────────────────────────────────────
def test_health_requires_endpoint_and_key():
    def check(base_url, has_key):
        return asyncio.run(check_provider(provider_type="azure_foundry",
                                          base_url=base_url, has_api_key=has_key))["status"]
    assert check(None, True) == "unconfigured"
    assert check("https://res.openai.azure.com", False) == "unconfigured"
    assert check("https://res.openai.azure.com", True) == "ok"
