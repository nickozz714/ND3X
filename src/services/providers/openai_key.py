"""Lazy OpenAI API-key resolution from the provider registry.

There is no global OpenAI key in config anymore. OpenAI is just a registry
provider: its key lives on the registered, enabled provider whose
provider_type == "openai", and is only needed when an OpenAI model is actually
called. If no OpenAI provider is registered, this returns None and OpenAI simply
isn't usable (the orchestrator stays provider-agnostic).
"""
from __future__ import annotations

from typing import Optional

from component.logging import get_logger

log = get_logger(__name__)


def registry_openai_api_key() -> Optional[str]:
    try:
        # Deferred imports keep this free of import cycles with the base service.
        from db.database import SessionLocal
        from services.providers.registry_service import ProviderRegistryService

        db = SessionLocal()
        try:
            reg = ProviderRegistryService(db)
            for p in reg.list_providers():
                if (getattr(p, "provider_type", "") or "") != "openai":
                    continue
                if getattr(p, "enabled", True) is False:
                    continue
                key = reg.get_api_key(p.id)
                if key:
                    return key
            return None
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — never break a caller on a registry hiccup
        log.warningx("OpenAI API key resolutie uit registry mislukt", error=str(exc))
        return None
