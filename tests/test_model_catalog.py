"""Tests for the online model catalog enrichment."""
from __future__ import annotations

from services.providers import model_catalog as mc


def test_build_maps_provider_keys_and_fields():
    raw = {
        "google": {"models": {"gemini-1.5-pro": {
            "name": "Gemini 1.5 Pro",
            "limit": {"context": 2000000, "output": 8192},
            "cost": {"input": 1.25, "output": 5.0},
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        }}},
        "voyageai": {"models": {"voyage-3": {
            "name": "Voyage 3", "limit": {"context": 32000}, "cost": {"input": 0.06},
            "modalities": {"input": ["text"], "output": ["text"]},
        }}},
        "unknownprov": {"models": {"x": {"name": "x"}}},
    }
    built = mc._build(raw)
    assert "gemini|gemini-1.5-pro" in built       # google -> gemini
    assert "voyage|voyage-3" in built             # voyageai -> voyage
    assert not any(k.startswith("unknownprov") for k in built)  # unmapped dropped
    g = built["gemini|gemini-1.5-pro"]
    assert g["context_window"] == 2000000
    assert g["price_in"] == 1.25 and g["price_out"] == 5.0
    assert g["capability"] == "chat"
    assert built["voyage|voyage-3"]["capability"] == "embeddings"  # voyage* -> embeddings


def test_enrich_uses_bundled_fallback_when_offline(monkeypatch):
    # Force the live fetch to fail and clear the cache.
    monkeypatch.setattr(mc, "_url", lambda: "http://127.0.0.1:9/none.json")
    mc._cache["data"] = None
    mc._cache["at"] = 0.0
    meta = mc.enrich("openai", "gpt-4o")
    assert meta and meta["display_name"] == "GPT-4o"
    assert meta["price_in"] == 2.5 and meta["price_out"] == 10.0


def test_enrich_normalizes_dated_ids(monkeypatch):
    mc._cache["data"] = {"anthropic|claude-3-5-sonnet": {
        "display_name": "Claude 3.5 Sonnet", "context_window": 200000,
        "price_in": 3.0, "price_out": 15.0, "capability": "chat", "good_for": "",
    }}
    mc._cache["at"] = 10 ** 12  # far future so it's "fresh"
    meta = mc.enrich("anthropic", "claude-3-5-sonnet-2024-10-22")
    assert meta.get("display_name") == "Claude 3.5 Sonnet"
    mc._cache["data"] = None


def test_enrich_unknown_returns_empty(monkeypatch):
    mc._cache["data"] = {}
    mc._cache["at"] = 10 ** 12
    assert mc.enrich("openai", "totally-made-up-model-xyz") == {}
    mc._cache["data"] = None


def test_capability_inference():
    assert mc._capability("text-embedding-3-small", {}) == "embeddings"
    assert mc._capability("whisper-1", {"input": ["audio"], "output": ["text"]}) == "transcription"
    assert mc._capability("gpt-4o-mini-tts", {"output": ["audio"]}) == "tts"
    assert mc._capability("gpt-4o-realtime", {}) == "realtime"
    assert mc._capability("gpt-4o", {"input": ["text"], "output": ["text"]}) == "chat"
