"""Dynamic local-model sizing: any model name gets a footprint + fit verdict,
not just the curated catalog."""
from __future__ import annotations

import pytest

from services.local_models.hardware import HardwareInfo
from services.local_models.recommendations import (
    build_recommendation,
    guess_capability,
    parse_params_b,
    recommend,
)


def _hw(usable=16.0):
    return HardwareInfo(os="Darwin", arch="arm64", cpu_cores=10, ram_gb=24.0, gpus=[],
                        unified_memory=True, disk_free_gb=200.0, usable_model_memory_gb=usable)


@pytest.mark.parametrize("name,expected", [
    ("qwen2.5:14b", 14.0),
    ("llama3.2:1b", 1.0),
    ("deepseek-r1:1.5b", 1.5),
    ("mixtral:8x7b", 56.0),
    ("gpt-oss:20b", 20.0),
    ("llama3.1:70b", 70.0),
    ("nomic-embed-text", None),
    ("weird-model", None),
])
def test_parse_params_b(name, expected):
    assert parse_params_b(name) == expected


def test_guess_capability():
    assert guess_capability("nomic-embed-text") == "embeddings"
    assert guess_capability("bge-m3") == "embeddings"
    assert guess_capability("qwen2.5:14b") == "chat"


def test_estimate_arbitrary_model_fits():
    r = build_recommendation(_hw(16.0), ollama_name="qwen2.5:14b")
    assert r.fits and r.verdict == "best" and r.estimated_gb > 0


def test_estimate_arbitrary_model_too_big():
    r = build_recommendation(_hw(16.0), ollama_name="llama3.1:70b")
    assert not r.fits and r.verdict == "won't fit" and r.warning


def test_estimate_unknown_size_is_usable_but_flagged():
    r = build_recommendation(_hw(16.0), ollama_name="brand-new-model")
    assert r.verdict == "unknown" and r.fits is True and r.warning


def test_estimate_embeddings():
    r = build_recommendation(_hw(16.0), ollama_name="nomic-embed-text")
    assert r.capability == "embeddings" and r.verdict == "embeddings" and r.fits


def test_recommend_merges_extra_names_and_dedupes():
    recs = recommend(_hw(16.0), capability="chat",
                     extra_names=["llama3.1:70b", "custom:32b", "qwen2.5:14b"])
    names = [r.ollama_name for r in recs]
    assert "custom:32b" in names           # discovered, not in catalog
    assert names.count("qwen2.5:14b") == 1  # dedup vs catalog
    # fitting models rank before the 70b that won't fit
    assert names.index("qwen2.5:14b") < names.index("llama3.1:70b")


def test_recommend_catalog_only_still_works():
    recs = recommend(_hw(16.0))
    assert recs and all(hasattr(r, "verdict") for r in recs)
