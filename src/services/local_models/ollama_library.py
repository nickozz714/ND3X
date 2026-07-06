"""
services/local_models/ollama_library.py

Best-effort discovery of pullable models from the public Ollama library so the
recommendations list stays current without code changes. Entirely optional:
every failure path returns an empty list, so callers fall back to the curated
catalog + locally-installed models.

Source is configurable via OLLAMA_LIBRARY_URL. It accepts either:
- a JSON array of names ("qwen2.5:7b") or objects ({"name": ...}), or
- the ollama.com/library HTML page (model slugs are scraped from /library/<name>).
Results are cached in-process for OLLAMA_LIBRARY_TTL seconds.
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional

from component.config import settings
from component.logging import get_logger

log = get_logger(__name__)

_DEFAULT_URL = "https://ollama.com/library"
_cache: dict = {"ts": 0.0, "names": []}
# name -> {"ts": float, "variants": list[str]}
_variants_cache: dict = {}

_SLUG_RE = re.compile(r'href="/library/([a-zA-Z0-9._-]+)"')
# A "primary" size tag, e.g. 7b, 14b, 0.5b, 32b, 72b — the default-quant pull for a
# given size (we deliberately skip the dozens of per-quant variants like 7b-q4_K_M).
_SIZE_TAG_RE = re.compile(r"^\d+(?:\.\d+)?b$")
# Tags that are not locally downloadable (run on Ollama's cloud).
_NON_PULLABLE_TAGS = {"cloud"}


def _size_sort_key(tag: str) -> float:
    m = re.match(r"(\d+(?:\.\d+)?)b", tag)
    return float(m.group(1)) if m else 0.0


def _ttl() -> float:
    return float(getattr(settings, "OLLAMA_LIBRARY_TTL", 3600) or 3600)


def _url() -> Optional[str]:
    # Empty/disabled by default-safe: only fetch when explicitly configured OR
    # when the default public library is reachable. Set OLLAMA_LIBRARY_URL="" to
    # disable entirely.
    val = getattr(settings, "OLLAMA_LIBRARY_URL", None)
    if val is None:
        return _DEFAULT_URL
    return val.strip() or None


def _parse(body: str, content_type: str) -> List[str]:
    names: List[str] = []
    text = body.strip()
    if "json" in (content_type or "").lower() or text[:1] in "[{":
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("models") or data.get("data") or []
            for it in items:
                if isinstance(it, str):
                    names.append(it)
                elif isinstance(it, dict):
                    n = it.get("name") or it.get("model") or it.get("id")
                    if n:
                        names.append(str(n))
            return names
        except Exception:  # noqa: BLE001 — fall through to HTML parse
            pass
    # HTML: scrape /library/<slug> links (base model names, no tag).
    for slug in dict.fromkeys(_SLUG_RE.findall(text)):
        names.append(slug)
    return names


def library_status() -> dict:
    """Current discovery state: whether enabled, the source URL, how many names
    are cached, and when they were fetched."""
    url = _url()
    return {
        "enabled": bool(url),
        "url": url,
        "count": len(_cache["names"]),
        "cached_at": _cache["ts"] or None,
    }


def fetch_library_names(*, force: bool = False) -> List[str]:
    """Cached list of model names from the configured library source. [] on any
    failure or when disabled."""
    url = _url()
    if not url:
        return []
    now = time.time()
    if not force and _cache["names"] and (now - _cache["ts"] < _ttl()):
        return _cache["names"]
    try:
        import httpx
        resp = httpx.get(url, timeout=5.0, follow_redirects=True,
                         headers={"User-Agent": "ND3X-LocalModels/1.0"})
        if resp.status_code != 200:
            return _cache["names"]
        names = _parse(resp.text, resp.headers.get("content-type", ""))
        if names:
            _cache["names"] = names
            _cache["ts"] = now
        return _cache["names"]
    except Exception as exc:  # noqa: BLE001 — never break recommendations on network
        log.debugx("Ollama library fetch mislukt; fallback op catalog", error=str(exc))
        return _cache["names"]


def fetch_model_variants(name: str, *, force: bool = False) -> List[str]:
    """Primary size variants (e.g. ['0.5b','7b','14b','32b','72b']) pullable for a
    base model, scraped from ollama.com/library/<name>/tags. Returns ['latest'] when
    a model exposes no explicit size tags, and [] on any failure. Cached per name."""
    name = (name or "").strip().split(":")[0]
    if not name or not _url():
        return []
    now = time.time()
    cached = _variants_cache.get(name)
    if not force and cached and (now - cached["ts"] < _ttl()):
        return cached["variants"]
    try:
        import httpx
        resp = httpx.get(
            f"https://ollama.com/library/{name}/tags",
            timeout=8.0, follow_redirects=True,
            headers={"User-Agent": "ND3X-LocalModels/1.0"},
        )
        if resp.status_code != 200:
            return cached["variants"] if cached else []
        tag_re = re.compile(r"/library/" + re.escape(name) + r":([a-zA-Z0-9._-]+)")
        all_tags = list(dict.fromkeys(tag_re.findall(resp.text)))
        # "cloud" tags are run on Ollama's cloud, not downloadable — exclude them so
        # we never offer a non-pullable variant (e.g. glm-4.7 is cloud-only).
        real_tags = [t for t in all_tags if t.lower() not in _NON_PULLABLE_TAGS]
        sizes = sorted({t for t in real_tags if _SIZE_TAG_RE.match(t)}, key=_size_sort_key)
        if sizes:
            variants = sizes
        elif "latest" in real_tags:
            variants = ["latest"]
        else:
            variants = []  # cloud-only / nothing downloadable
        _variants_cache[name] = {"ts": now, "variants": variants}
        return variants
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        log.debugx("Ollama model variants fetch mislukt", model=name, error=str(exc))
        return cached["variants"] if cached else []
