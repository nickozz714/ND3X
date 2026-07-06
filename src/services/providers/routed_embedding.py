"""
services/providers/routed_embedding.py

A drop-in wrapper around the base (OpenAI) embedding service for the long-lived
text indexing/search singletons. On first use it resolves the 'embeddings'
capability slot (one short-lived DB session) and caches the decision:
  - a configured non-OpenAI embedding provider, or
  - the base service (default; no slot assigned).

Caching avoids a DB session per chunk during indexing. Changing the embeddings
slot takes effect on restart (and changing embedding model/dimension requires a
FAISS re-index — see the design plan's risk #2).
"""
from __future__ import annotations

from typing import Any, List, Optional

from component.logging import get_logger
from services.providers.base import EmbeddingProvider

log = get_logger(__name__)


class RoutedEmbeddingService:
    def __init__(self, base: Any):
        self._base = base
        self._resolved: Optional[EmbeddingProvider] = None
        # Model id from the "embeddings" slot. The base (OpenAI) service is built with
        # embedding_model=None, so when we fall back to it we must pass the slot model
        # explicitly — otherwise the API rejects the call ("must provide a model").
        self._model: Optional[str] = None
        self._checked = False

    def _provider(self) -> Optional[EmbeddingProvider]:
        if not self._checked:
            self._checked = True
            try:
                from db.database import SessionLocal
                from services.providers.provider_factory import resolve_embedding_provider
                from services.providers.registry_service import ProviderRegistryService
                db = SessionLocal()
                try:
                    self._resolved = resolve_embedding_provider(db, self._base)
                    try:
                        slot = ProviderRegistryService(db).resolve_slot("embeddings")
                        self._model = getattr(slot, "model_id", None) if slot else None
                    except Exception:  # noqa: BLE001
                        self._model = None
                finally:
                    db.close()
                if self._resolved is not None:
                    log.infox("Embeddings via geconfigureerde provider", provider=getattr(self._resolved, "provider_type", "?"))
                else:
                    log.infox("Embeddings via base (OpenAI) met slot-model", model=self._model)
            except Exception as exc:  # noqa: BLE001 — never break indexing
                log.warningx("Embedding routing init mislukt; OpenAI fallback", error=str(exc))
                self._resolved = None
        return self._resolved

    def _base_kwargs(self, kwargs: dict) -> dict:
        # Ensure the base (OpenAI) embed call carries the embeddings-slot model.
        if self._model and "model" not in kwargs:
            return {**kwargs, "model": self._model}
        return kwargs

    def embed(self, text: str, **kwargs) -> List[float]:
        prov = self._provider()
        if prov is not None:
            return prov.embed(text)
        return self._base.embed(text, **self._base_kwargs(kwargs))

    def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        prov = self._provider()
        if prov is not None:
            return prov.embed_batch(texts)
        return self._base.embed_batch(texts, **self._base_kwargs(kwargs))

    def __getattr__(self, name: str) -> Any:
        # cosine_similarity and everything else delegate to the base service.
        return getattr(self._base, name)
