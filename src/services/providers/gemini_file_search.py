from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from services.providers.registry_service import ProviderRegistryService


async def mirror_thread_files_to_gemini(
    *, db: Any, thread_dir: Path, thread_id: str, paths: list[str]
) -> dict[str, str]:
    registry = ProviderRegistryService(db)
    stores: dict[str, str] = {}
    providers = [
        provider for provider in registry.list_providers()
        if provider.enabled and provider.provider_type == "gemini"
    ]
    for provider in providers:
        api_key = registry.get_api_key(provider.id)
        if not api_key:
            continue
        try:
            store_name = await asyncio.to_thread(
                _mirror_provider,
                api_key=api_key,
                provider_id=provider.id,
                thread_dir=thread_dir,
                thread_id=thread_id,
                paths=paths,
            )
            stores[str(provider.id)] = store_name
        except Exception:
            continue
    return stores


def _mirror_provider(
    *, api_key: str, provider_id: int, thread_dir: Path, thread_id: str, paths: list[str]
) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)
    manifest = thread_dir / f"gemini_file_search_{provider_id}.json"
    if manifest.exists():
        store_name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
    else:
        store = client.file_search_stores.create(config={
            "display_name": f"nd3x-thread-{thread_id[:48]}",
            "embedding_model": "models/gemini-embedding-2",
        })
        store_name = store.name
        manifest.write_text(json.dumps({"name": store_name}), encoding="utf-8")
    for path in paths:
        operation = client.file_search_stores.upload_to_file_search_store(
            file=path,
            file_search_store_name=store_name,
            config={"display_name": Path(path).name},
        )
        while not operation.done:
            time.sleep(2)
            operation = client.operations.get(operation)
    return store_name

