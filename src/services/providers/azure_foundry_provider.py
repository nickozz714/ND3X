"""
services/providers/azure_foundry_provider.py

Adapter for Azure AI Foundry (Microsoft Foundry Models) via the **v1 GA
OpenAI-compatible API** — Azure OpenAI models (GPT-4o/4.1/o-series) plus
Azure-sold open models (DeepSeek, Grok, MAI, Llama, Phi, Mistral) behind one
resource endpoint.

Key facts the adapter encodes (Microsoft Learn, 2026-07):
- The v1 route lives at ``https://<resource>.openai.azure.com/openai/v1`` (the
  ``.services.ai.azure.com`` host works too). No ``api-version`` query param.
- The standard ``openai`` SDK is the supported client (``AzureOpenAI()`` and the
  deprecated ``azure-ai-inference`` SDK are NOT needed/used).
- ``model`` = the **deployment name**, not the model name.
- Auth (phase 1): Azure API key, sent as the SDK's regular bearer credential —
  the v1 endpoint accepts both ``Authorization: Bearer`` and ``api-key``.
  Entra ID keyless (token provider) is a possible later phase.

Behavior is inherited from the OpenAI-compatible adapter (Chat Completions +
Embeddings); this subclass adds Foundry base-url normalization and cloud-tuned
retries.
"""
from __future__ import annotations

from typing import Any, Optional

from component.logging import get_logger
from services.providers.openai_compatible_provider import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
    _compatible_timeout,
)

log = get_logger(__name__)

# Hostname markers of a Foundry / Azure OpenAI resource endpoint.
_FOUNDRY_HOSTS = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")


def normalize_foundry_base_url(base_url: Optional[str]) -> Optional[str]:
    """Normalize a pasted Foundry endpoint to the OpenAI-compatible v1 route.

    Users paste the resource endpoint from the Azure portal
    (``https://<resource>.openai.azure.com``); the OpenAI-compatible API lives
    under ``/openai/v1``. Rules (idempotent, conservative):
    - no path (or ``/``)      → append ``/openai/v1``
    - path ``/openai``        → append ``/v1``
    - path already ``/openai/v1[...]`` or anything else → unchanged (custom
      gateways/APIM fronts keep whatever path was configured).
    """
    if not base_url:
        return base_url
    url = base_url.strip().rstrip("/")
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        path = (urlparse(url).path or "").rstrip("/")
    except Exception:  # noqa: BLE001 — never block on a weird URL; use as-is
        return url
    if path == "":
        return f"{url}/openai/v1"
    if path == "/openai":
        return f"{url}/v1"
    return url


def _build_foundry_async_client(base_url: Optional[str], api_key: Optional[str]) -> Any:
    from openai import AsyncOpenAI  # lazy import
    # Unlike local/compatible endpoints (max_retries=0: a stuck local model won't
    # succeed on retry), Foundry is a cloud API where transient 429/5xx are
    # normal — keep the SDK's backoff retries.
    return AsyncOpenAI(
        base_url=base_url, api_key=api_key,
        timeout=_compatible_timeout(), max_retries=2,
    )


def _build_foundry_sync_client(base_url: Optional[str], api_key: Optional[str]) -> Any:
    from openai import OpenAI  # lazy import
    return OpenAI(
        base_url=base_url, api_key=api_key,
        timeout=_compatible_timeout(), max_retries=2,
    )


class AzureFoundryChatProvider(OpenAICompatibleChatProvider):
    """Chat via a Foundry deployment. ``default_model``/``model`` = deployment name."""

    provider_type = "azure_foundry"

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None,
                 default_model: str = "", client: Any = None):
        normalized = normalize_foundry_base_url(base_url)
        super().__init__(
            base_url=normalized, api_key=api_key, default_model=default_model,
            client=client if client is not None else _build_foundry_async_client(normalized, api_key),
        )


class AzureFoundryEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    """Embeddings via a Foundry deployment (e.g. text-embedding-3-large)."""

    provider_type = "azure_foundry"

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None,
                 default_model: str = "", client: Any = None):
        normalized = normalize_foundry_base_url(base_url)
        super().__init__(
            base_url=normalized, api_key=api_key, default_model=default_model,
            client=client if client is not None else _build_foundry_sync_client(normalized, api_key),
        )
