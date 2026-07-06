"""Online model catalog — enriches discovered cloud models with metadata.

Provider list-APIs (OpenAI/Anthropic) return only model ids. To show users a
helpful picker (name, context window, price, what it's good for) we pull a public,
no-auth catalog (models.dev) and merge it by (provider_type, model_id). The fetch
is cached in-memory with a TTL and falls back to a small bundled catalog when the
network is unavailable, so the UI never blocks.

Pricing is indicative (USD per 1M tokens), sourced from the catalog.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from component.config import settings
from component.logging import get_logger

log = get_logger(__name__)

# models.dev provider key -> our provider_type
_PROVIDER_KEY_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "voy ageai": "voyage",  # placeholder guard (never matches)
    "voyageai": "voyage",
    "voyage": "voyage",
}

# Minimal offline fallback (USD per 1M tokens). Used only when the live fetch fails.
_BUNDLED: Dict[str, Dict[str, Any]] = {
    "openai|gpt-4o": {"display_name": "GPT-4o", "context_window": 128000, "price_in": 2.5, "price_out": 10.0, "capability": "chat", "good_for": "Multimodal flagship · 128K ctx"},
    "openai|gpt-4o-mini": {"display_name": "GPT-4o mini", "context_window": 128000, "price_in": 0.15, "price_out": 0.6, "capability": "chat", "good_for": "Cheap, fast · 128K ctx"},
    "openai|o3": {"display_name": "o3", "context_window": 200000, "price_in": 2.0, "price_out": 8.0, "capability": "chat", "good_for": "Reasoning · 200K ctx"},
    "openai|text-embedding-3-small": {"display_name": "text-embedding-3-small", "context_window": 8191, "price_in": 0.02, "price_out": 0.0, "capability": "embeddings", "good_for": "Cheap embeddings"},
    "openai|text-embedding-3-large": {"display_name": "text-embedding-3-large", "context_window": 8191, "price_in": 0.13, "price_out": 0.0, "capability": "embeddings", "good_for": "High-quality embeddings"},
    "openai|whisper-1": {"display_name": "Whisper", "context_window": 0, "price_in": 0.0, "price_out": 0.0, "capability": "transcription", "good_for": "Speech-to-text"},
    "openai|gpt-4o-mini-tts": {"display_name": "GPT-4o mini TTS", "context_window": 0, "price_in": 0.0, "price_out": 0.0, "capability": "tts", "good_for": "Text-to-speech"},
    "anthropic|claude-3-5-sonnet-latest": {"display_name": "Claude 3.5 Sonnet", "context_window": 200000, "price_in": 3.0, "price_out": 15.0, "capability": "chat", "good_for": "Strong general agent · 200K ctx"},
    "anthropic|claude-3-5-haiku-latest": {"display_name": "Claude 3.5 Haiku", "context_window": 200000, "price_in": 0.8, "price_out": 4.0, "capability": "chat", "good_for": "Fast, cheap · 200K ctx"},
    "gemini|gemini-1.5-pro": {"display_name": "Gemini 1.5 Pro", "context_window": 2000000, "price_in": 1.25, "price_out": 5.0, "capability": "chat", "good_for": "Huge 2M ctx · multimodal"},
    "gemini|gemini-1.5-flash": {"display_name": "Gemini 1.5 Flash", "context_window": 1000000, "price_in": 0.075, "price_out": 0.3, "capability": "chat", "good_for": "Cheap, 1M ctx"},
    "gemini|text-embedding-004": {"display_name": "Gemini text-embedding-004", "context_window": 2048, "price_in": 0.0, "price_out": 0.0, "capability": "embeddings", "good_for": "Embeddings"},
    "voyage|voyage-3": {"display_name": "Voyage 3", "context_window": 32000, "price_in": 0.06, "price_out": 0.0, "capability": "embeddings", "good_for": "Retrieval embeddings"},
}

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _ttl() -> int:
    return int(getattr(settings, "MODEL_CATALOG_TTL", 86400) or 86400)


def _url() -> str:
    return getattr(settings, "MODEL_CATALOG_URL", "") or "https://models.dev/api.json"


def _norm(model_id: str) -> str:
    """Normalise an id for matching: lowercase, drop a trailing -YYYY-MM-DD date."""
    m = (model_id or "").strip().lower()
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", m)


def _capability(model_id: str, modalities: Dict[str, Any]) -> str:
    mid = model_id.lower()
    out = [str(x).lower() for x in (modalities.get("output") or [])]
    inp = [str(x).lower() for x in (modalities.get("input") or [])]
    if "embed" in mid or "embedding" in mid or mid.startswith("voyage"):
        return "embeddings"
    if "realtime" in mid:
        return "realtime"
    if "tts" in mid or "speech" in mid or "audio" in out:
        return "tts"
    if "whisper" in mid or "transcribe" in mid or ("audio" in inp and out == ["text"]):
        return "transcription"
    return "chat"


def _good_for(m: Dict[str, Any]) -> str:
    bits = []
    if m.get("reasoning"):
        bits.append("reasoning")
    inp = [str(x).lower() for x in ((m.get("modalities") or {}).get("input") or [])]
    if any(x in inp for x in ("image", "pdf", "audio")):
        bits.append("multimodal")
    ctx = ((m.get("limit") or {}).get("context"))
    if ctx:
        bits.append(f"{int(ctx)//1000}K ctx" if ctx >= 1000 else f"{ctx} ctx")
    return " · ".join(bits)


def _build(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for pkey, prov in (raw or {}).items():
        ptype = _PROVIDER_KEY_MAP.get(str(pkey).lower())
        if not ptype or not isinstance(prov, dict):
            continue
        for mid, m in (prov.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            cost = m.get("cost") or {}
            limit = m.get("limit") or {}
            out[f"{ptype}|{_norm(mid)}"] = {
                "display_name": m.get("name") or mid,
                "context_window": int(limit.get("context") or 0) or None,
                "price_in": cost.get("input"),
                "price_out": cost.get("output"),
                "capability": _capability(mid, m.get("modalities") or {}),
                "good_for": _good_for(m),
                "modalities": m.get("modalities") or {},
            }
    return out


def fetch_catalog(*, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Return the normalised catalog keyed by 'provider_type|normalised_id'.
    Cached for MODEL_CATALOG_TTL; bundled fallback on failure."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["at"]) < _ttl():
        return _cache["data"]
    try:
        import httpx
        r = httpx.get(_url(), timeout=12.0)
        r.raise_for_status()
        data = _build(r.json())
        if data:
            _cache["data"] = data
            _cache["at"] = now
            log.infox("Model catalog opgehaald", entries=len(data), url=_url())
            return data
    except Exception as exc:  # noqa: BLE001 — catalog is best-effort
        log.warningx("Model catalog ophalen mislukt; bundled fallback", error=str(exc))
    if _cache["data"] is not None:
        return _cache["data"]
    return dict(_BUNDLED)


def enrich(provider_type: str, model_id: str) -> Dict[str, Any]:
    """Metadata for a model, or {} if unknown."""
    catalog = fetch_catalog()
    key = f"{(provider_type or '').strip().lower()}|{_norm(model_id)}"
    if key in catalog:
        return dict(catalog[key])
    if key in _BUNDLED:
        return dict(_BUNDLED[key])
    return {}


def catalog_for_provider(provider_type: str) -> list[Dict[str, Any]]:
    """All known catalog models for a provider type (for the picker even before a
    live list is available)."""
    pt = (provider_type or "").strip().lower()
    catalog = fetch_catalog()
    out = []
    for key, meta in catalog.items():
        if key.startswith(pt + "|"):
            out.append({"model_id": key.split("|", 1)[1], **meta})
    out.sort(key=lambda x: (x.get("capability", ""), x.get("model_id", "")))
    return out
