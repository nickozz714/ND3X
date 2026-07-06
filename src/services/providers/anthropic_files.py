from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from services.providers.registry_service import ProviderRegistryService


async def upload_files_to_anthropic(*, db: Any, paths: list[str]) -> dict[str, list[str]]:
    registry = ProviderRegistryService(db)
    uploaded: dict[str, list[str]] = {}
    providers = [
        provider for provider in registry.list_providers()
        if provider.enabled and provider.provider_type == "anthropic"
    ]
    for provider in providers:
        api_key = registry.get_api_key(provider.id)
        if not api_key:
            continue
        try:
            ids = await asyncio.to_thread(_upload, api_key, paths)
            uploaded[str(provider.id)] = ids
        except Exception:
            continue
    return uploaded


def _upload(api_key: str, paths: list[str]) -> list[str]:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    result: list[str] = []
    for path in paths:
        metadata = client.beta.files.upload(
            file=Path(path),
            betas=["files-api-2025-04-14"],
        )
        result.append(metadata.id)
    return result

