"""
services/local_models/recommendations.py

Ranks local models best→worst for a given HardwareInfo and annotates each with
what it's good for, an estimated footprint, and a fit verdict/warning.

Heuristic: a 4-bit (Q4_K_M) model's weights ≈ params(B) × 0.6 GB, plus ~1.5 GB
runtime/context overhead. A model "fits" when that estimate ≤ usable budget.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from services.local_models.hardware import HardwareInfo

_Q4_GB_PER_B = 0.6
_OVERHEAD_GB = 1.5

# Parameter-size parsing from an Ollama tag (e.g. "qwen2.5:14b" -> 14,
# "mixtral:8x7b" -> 56, "deepseek-r1:1.5b" -> 1.5). Lets us size ANY model the
# user names, not just the curated catalog.
_MOE_RE = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
_EMBED_HINT = ("embed", "bge", "nomic", "minilm", "gte", "e5")


def parse_params_b(name: str) -> Optional[float]:
    """Best-effort billions-of-parameters from a model tag, or None if unknown."""
    if not name:
        return None
    n = name.lower()
    moe = _MOE_RE.search(n)
    if moe:
        return round(float(moe.group(1)) * float(moe.group(2)), 1)
    # Search the tag portion (after ':') first, then the whole name.
    tail = n.split(":", 1)[1] if ":" in n else n
    for hay in (tail, n):
        m = _SIZE_RE.search(hay)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def guess_capability(name: str) -> str:
    n = (name or "").lower()
    return "embeddings" if any(h in n for h in _EMBED_HINT) else "chat"


@dataclass
class CatalogModel:
    ollama_name: str          # e.g. "qwen2.5:7b"
    display_name: str
    params_b: float
    capability: str           # chat | embeddings
    good_for: str


# Curated catalog (Ollama names). Ordered roughly by capability tier within size.
CATALOG: List[CatalogModel] = [
    CatalogModel("qwen2.5:14b", "Qwen2.5 14B", 14, "chat",
                 "Strongest local reasoning/coding that still fits a 24GB machine; good for planners and complex Q&A."),
    CatalogModel("qwen2.5:7b", "Qwen2.5 7B", 7, "chat",
                 "Excellent all-rounder: daily-driver chat, planners, and RAG. Best quality/size balance."),
    CatalogModel("qwen2.5-coder:7b", "Qwen2.5 Coder 7B", 7, "chat",
                 "Coding-focused: code generation, review, and tool-calling agents."),
    CatalogModel("llama3.1:8b", "Llama 3.1 8B", 8, "chat",
                 "Strong general chat and instruction following; broad ecosystem support."),
    CatalogModel("gemma2:9b", "Gemma 2 9B", 9, "chat",
                 "Solid reasoning and summarization; good knowledge-work assistant."),
    CatalogModel("phi4:14b", "Phi-4 14B", 14, "chat",
                 "Punchy reasoning for its size; strong math/logic."),
    CatalogModel("qwen2.5:3b", "Qwen2.5 3B", 3, "chat",
                 "Very fast: subagents, classification, and the ad-hoc dispatch role."),
    CatalogModel("llama3.2:3b", "Llama 3.2 3B", 3, "chat",
                 "Fast lightweight chat for constrained machines."),
    CatalogModel("nomic-embed-text", "Nomic Embed Text", 0.14, "embeddings",
                 "Local text embeddings — replace cloud embeddings for offline RAG."),
    CatalogModel("bge-m3", "BGE-M3", 0.57, "embeddings",
                 "Multilingual local embeddings with strong retrieval quality."),
]


@dataclass
class ModelRecommendation:
    ollama_name: str
    display_name: str
    capability: str
    params_b: float
    estimated_gb: float
    fits: bool
    verdict: str       # "best" | "great" | "good" | "fast" | "won't fit" | "embeddings"
    good_for: str
    warning: Optional[str] = None


def estimate_footprint_gb(params_b: float) -> float:
    return round(params_b * _Q4_GB_PER_B + _OVERHEAD_GB, 1)


def _catalog_by_name() -> Dict[str, CatalogModel]:
    return {m.ollama_name: m for m in CATALOG}


def build_recommendation(
    hw: HardwareInfo,
    *,
    ollama_name: str,
    capability: Optional[str] = None,
    params_b: Optional[float] = None,
    display_name: Optional[str] = None,
    good_for: str = "",
) -> ModelRecommendation:
    """Footprint + fit verdict for ANY model name against the given hardware.
    Curated catalog entries supply nicer metadata; everything else is sized from
    the tag (params_b) and falls back to an 'unknown' verdict if unparseable."""
    cat = _catalog_by_name().get(ollama_name)
    if cat:
        params_b = cat.params_b if params_b is None else params_b
        capability = capability or cat.capability
        display_name = display_name or cat.display_name
        good_for = good_for or cat.good_for
    if params_b is None:
        params_b = parse_params_b(ollama_name)
    capability = capability or guess_capability(ollama_name)
    display_name = display_name or ollama_name

    budget = max(0.0, float(hw.usable_model_memory_gb or 0.0))
    warning: Optional[str] = None

    if capability == "embeddings":
        est = estimate_footprint_gb(params_b) if params_b else 0.5
        return ModelRecommendation(
            ollama_name=ollama_name, display_name=display_name, capability="embeddings",
            params_b=params_b or 0.0, estimated_gb=est, fits=True, verdict="embeddings",
            good_for=good_for, warning=None,
        )

    if params_b is None:
        # Unknown size — let the user try, but say we couldn't size it.
        return ModelRecommendation(
            ollama_name=ollama_name, display_name=display_name, capability="chat",
            params_b=0.0, estimated_gb=0.0, fits=True, verdict="unknown",
            good_for=good_for,
            warning="Couldn't infer the parameter size from the name — footprint unknown.",
        )

    est = estimate_footprint_gb(params_b)
    fits = est <= budget if budget > 0 else False
    if not fits:
        verdict = "won't fit"
        warning = f"Needs ~{est} GB but only ~{budget} GB usable — would swap/run very slowly."
    elif params_b >= 13:
        verdict = "best"
    elif params_b >= 7:
        verdict = "great"
    elif params_b >= 4:
        verdict = "good"
    else:
        verdict = "fast"
    return ModelRecommendation(
        ollama_name=ollama_name, display_name=display_name, capability="chat",
        params_b=params_b, estimated_gb=est, fits=fits, verdict=verdict,
        good_for=good_for, warning=warning,
    )


def recommend(
    hw: HardwareInfo,
    *,
    capability: Optional[str] = None,
    extra_names: Optional[List[str]] = None,
    include_catalog: bool = True,
) -> List[ModelRecommendation]:
    """Ranked recommendations. Merges the curated catalog with any `extra_names`
    discovered live (installed models, a remote library, user-typed names),
    de-duplicated, each sized + fit-checked against `hw`."""
    names: List[str] = []
    if include_catalog:
        names.extend(m.ollama_name for m in CATALOG)
    for n in (extra_names or []):
        n = (n or "").strip()
        if n and n not in names:
            names.append(n)

    out: List[ModelRecommendation] = []
    for name in names:
        rec = build_recommendation(hw, ollama_name=name)
        if capability and rec.capability != capability:
            continue
        out.append(rec)

    # Rank: chat models that fit first (largest first = most capable), then
    # unknown-size, then non-fitting chat, then embeddings.
    def sort_key(r: ModelRecommendation):
        if r.capability != "chat":
            tier = 3
        elif r.fits and r.verdict != "unknown":
            tier = 0
        elif r.verdict == "unknown":
            tier = 1
        else:
            tier = 2
        return (tier, -r.params_b)

    out.sort(key=sort_key)
    return out
