"""
services/providers/model_discovery.py

Best-effort discovery of the models a configured provider exposes, so users don't
have to hand-type model ids. Queries the provider's own listing API:
- openai / openai_compatible: GET {base_url}/models
- anthropic:                  GET https://api.anthropic.com/v1/models
- ollama:                     GET {host}/api/tags (installed local models)

Capability is guessed from the model id. Every failure returns an empty list with
an error message rather than raising.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_EMBED_HINT = ("embed", "bge", "nomic", "minilm", "gte", "e5", "voyage")
_STT_HINT = ("whisper", "transcribe", "stt")
_TTS_HINT = ("tts", "speech", "audio-speech")


def guess_capability(model_id: str) -> str:
    m = (model_id or "").lower()
    if any(h in m for h in _EMBED_HINT):
        return "embeddings"
    if any(h in m for h in _STT_HINT):
        return "transcription"
    if any(h in m for h in _TTS_HINT):
        return "tts"
    return "chat"


def _shape(ids: List[str], provider_type: str = "") -> List[Dict[str, Any]]:
    """Shape discovered ids into rich rows, enriched from the online model catalog
    (display name, context window, price, what it's good for) when available."""
    from services.providers.model_catalog import enrich

    seen = set()
    out: List[Dict[str, Any]] = []
    for mid in ids:
        mid = (mid or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        meta = enrich(provider_type, mid)
        out.append({
            "model_id": mid,
            # Catalog capability is more reliable than the id keyword guess.
            "capability": meta.get("capability") or guess_capability(mid),
            "display_name": meta.get("display_name"),
            "context_window": meta.get("context_window"),
            "price_in": meta.get("price_in"),
            "price_out": meta.get("price_out"),
            "good_for": meta.get("good_for"),
            "in_catalog": bool(meta),
        })
    out.sort(key=lambda x: (x["capability"], x["model_id"]))
    return out


def discover_models(
    *, provider_type: str, base_url: Optional[str], api_key: Optional[str],
) -> Dict[str, Any]:
    t = (provider_type or "").strip()
    if t == "claude_code":
        # No listing API — the CLI takes model aliases; latest per tier.
        # fable = Claude Fable 5, the top tier (verified live: `--model fable`).
        return {"models": _shape(["fable", "opus", "sonnet", "haiku"], t)}
    try:
        import httpx
        if t == "anthropic":
            if not api_key:
                return {"models": [], "error": "No API key set for this provider."}
            r = httpx.get(f"{_ANTHROPIC_BASE}/models", timeout=10.0,
                          headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            return {"models": _shape(ids, t)}

        if t == "ollama":
            # base_url is like http://host:11434/v1 -> strip /v1 for the native API
            host = (base_url or "http://localhost:11434/v1").rsplit("/v1", 1)[0]
            r = httpx.get(f"{host}/api/tags", timeout=10.0)
            r.raise_for_status()
            ids = [m.get("name") for m in (r.json().get("models") or []) if m.get("name")]
            return {"models": _shape(ids, t)}

        if t == "azure_foundry":
            # Prefer the resource's DEPLOYMENTS: their ids are the deployment
            # names — exactly what ND3X must register as model_id. (The v1
            # {base}/models route returns the whole Foundry model CATALOG, not
            # what this resource can serve; live-verified 2026-07-17.) The
            # endpoint accepts both auth headers; send both.
            from services.providers.azure_foundry_provider import normalize_foundry_base_url
            if not api_key:
                return {"models": [], "error": "No API key set for this provider."}
            base = (normalize_foundry_base_url(base_url) or "").rstrip("/")
            if not base:
                return {"models": [], "error": "No base URL set for this provider."}
            headers = {"Authorization": f"Bearer {api_key}", "api-key": api_key}
            root = base[: -len("/openai/v1")] if base.endswith("/openai/v1") else base
            try:
                r = httpx.get(f"{root}/openai/deployments", timeout=10.0, headers=headers,
                              params={"api-version": "2023-03-15-preview"})
                r.raise_for_status()
                items = r.json().get("data") or []
                ids = [i.get("id") for i in items if isinstance(i, dict) and i.get("id")]
                if ids:
                    return {"models": _shape(ids, t)}
            except Exception as exc:  # noqa: BLE001 — fall back to the v1 catalog
                log.warningx("Foundry deployments-listing mislukt; val terug op catalogus",
                             error=str(exc))
            r = httpx.get(f"{base}/models", timeout=10.0, headers=headers)
            r.raise_for_status()
            data = r.json()
            items = data.get("data") if isinstance(data, dict) else data
            ids = [m.get("id") if isinstance(m, dict) else m for m in (items or [])]
            return {"models": _shape([i for i in ids if i], t)}

        # openai + openai_compatible (+ gemini/voyage compatible endpoints)
        base = (base_url or _OPENAI_BASE).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{base}/models", timeout=10.0, headers=headers)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        ids = [m.get("id") if isinstance(m, dict) else m for m in (items or [])]
        return {"models": _shape([i for i in ids if i], t)}
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        log.warningx("Model discovery mislukt", provider_type=t, error=str(exc))
        return {"models": [], "error": f"Could not list models: {exc}"}
