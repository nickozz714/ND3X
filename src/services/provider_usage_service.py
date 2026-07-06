"""Provider-reported **actual** usage & cost (the truth layer).

The local token ledger (`usage_service`) is a best-effort *estimate* reconstructed
from recorded chat turns — it misses embeddings/transcription/realtime, cached-token
discounts, and any unrecorded turn, so it can't match a provider invoice. This
module pulls each provider's **actual** organization usage + cost from its admin
API, broken down by model, so the dashboard can show truth where available and
tag everything else as an estimate.

Provider support:
  - openai    : /v1/organization/usage/completions (tokens by model) + /v1/organization/costs (USD)
  - anthropic : /v1/organizations/usage_report/messages (tokens) + /v1/organizations/cost_report (USD)
  - gemini, voyage : no per-model usage API → not available (billing via cloud console)
  - ollama / local : free → handled as an estimate (cost 0) by the caller

Each remote provider needs an **Admin** key (org-scoped) — set per provider in the
AI Models tab (`Provider.admin_api_key_encrypted`); falls back to the provider's
normal key, which usually 401s on these org endpoints. No LLM calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

_OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
_OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
_ANTHROPIC_USAGE_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"
_ANTHROPIC_COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"

# Provider types that expose an org usage/cost API.
TRUTH_CAPABLE = ("openai", "anthropic")


def _month_start_ts() -> int:
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())


def _price_map(db: Session) -> Dict[str, tuple]:
    from services.usage_service import UsageService
    return UsageService(db)._price_map()  # noqa: SLF001 — internal reuse


def _cost_of(input_tokens: int, output_tokens: int, price: Optional[tuple]) -> Optional[float]:
    from services.usage_service import UsageService
    return UsageService._cost_of(input_tokens, output_tokens, price)  # noqa: SLF001


# ── OpenAI ───────────────────────────────────────────────────────────────────
def _openai_actuals(key: str, start: int, price: Dict[str, tuple]) -> Dict[str, Any]:
    import httpx
    by_model: Dict[str, Dict[str, int]] = {}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=20.0) as client:
            # Tokens by model (actual).
            page: Optional[str] = None
            for _ in range(40):
                params: Dict[str, Any] = {
                    "start_time": start, "bucket_width": "1d", "group_by": "model", "limit": 31,
                }
                if page:
                    params["page"] = page
                r = client.get(_OPENAI_USAGE_URL, params=params, headers=headers)
                if r.status_code in (401, 403):
                    return {"available": False, "needs_key": True,
                            "error": "OpenAI rejected the key — the usage API needs an Admin key (org owner, api.usage.read)."}
                r.raise_for_status()
                data = r.json()
                for bucket in data.get("data", []):
                    for res in bucket.get("results", []):
                        m = res.get("model") or "unknown"
                        agg = by_model.setdefault(m, {"input_tokens": 0, "output_tokens": 0})
                        agg["input_tokens"] += int(res.get("input_tokens") or 0)
                        agg["output_tokens"] += int(res.get("output_tokens") or 0)
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break
            # Total cost (actual $).
            total_cost = 0.0
            page = None
            for _ in range(40):
                params = {"start_time": start, "bucket_width": "1d", "limit": 31}
                if page:
                    params["page"] = page
                r = client.get(_OPENAI_COSTS_URL, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
                for bucket in data.get("data", []):
                    for res in bucket.get("results", []):
                        total_cost += float((res.get("amount") or {}).get("value") or 0.0)
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break
    except Exception as exc:  # noqa: BLE001
        log.warningx("OpenAI usage ophalen mislukt", error=str(exc))
        return {"available": False, "needs_key": False, "error": f"Could not fetch OpenAI usage: {exc}"}
    return _shape(by_model, total_cost, price)


# ── Anthropic ────────────────────────────────────────────────────────────────
def _anthropic_actuals(key: str, start: int, price: Dict[str, tuple]) -> Dict[str, Any]:
    import httpx
    by_model: Dict[str, Dict[str, int]] = {}
    starting_at = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        with httpx.Client(timeout=20.0) as client:
            page: Optional[str] = None
            for _ in range(40):
                params: Dict[str, Any] = {
                    "starting_at": starting_at, "bucket_width": "1d", "group_by[]": "model", "limit": 31,
                }
                if page:
                    params["page"] = page
                r = client.get(_ANTHROPIC_USAGE_URL, params=params, headers=headers)
                if r.status_code in (401, 403):
                    return {"available": False, "needs_key": True,
                            "error": "Anthropic rejected the key — the usage API needs an Admin key (sk-ant-admin…)."}
                r.raise_for_status()
                data = r.json()
                for bucket in data.get("data", []):
                    for res in bucket.get("results", []):
                        m = res.get("model") or "unknown"
                        agg = by_model.setdefault(m, {"input_tokens": 0, "output_tokens": 0})
                        agg["input_tokens"] += (
                            int(res.get("uncached_input_tokens") or 0)
                            + int(res.get("cache_creation_input_tokens") or 0)
                            + int(res.get("cache_read_input_tokens") or 0)
                        )
                        agg["output_tokens"] += int(res.get("output_tokens") or 0)
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break
            # Total cost — amounts are decimal strings in the currency's lowest unit (cents).
            total_cost = 0.0
            page = None
            for _ in range(40):
                params = {"starting_at": starting_at, "bucket_width": "1d", "limit": 31}
                if page:
                    params["page"] = page
                r = client.get(_ANTHROPIC_COST_URL, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
                for bucket in data.get("data", []):
                    for res in bucket.get("results", []):
                        amt = res.get("amount")
                        val = amt.get("value") if isinstance(amt, dict) else amt
                        try:
                            total_cost += float(val) / 100.0  # cents → USD
                        except (TypeError, ValueError):
                            pass
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break
    except Exception as exc:  # noqa: BLE001
        log.warningx("Anthropic usage ophalen mislukt", error=str(exc))
        return {"available": False, "needs_key": False, "error": f"Could not fetch Anthropic usage: {exc}"}
    return _shape(by_model, total_cost, price)


def _shape(by_model: Dict[str, Dict[str, int]], total_cost: float, price: Dict[str, tuple]) -> Dict[str, Any]:
    models = []
    tot_in = tot_out = 0
    for m, agg in by_model.items():
        i, o = int(agg["input_tokens"]), int(agg["output_tokens"])
        tot_in += i
        tot_out += o
        models.append({
            "model": m, "input_tokens": i, "output_tokens": o,
            "total_tokens": i + o,
            # Per-model cost on REAL tokens via the local price map (per-model split
            # is approximate; the provider total below is the authoritative $).
            "cost_usd": _cost_of(i, o, price.get(m)),
        })
    models.sort(key=lambda x: -(x["total_tokens"]))
    return {
        "available": True, "needs_key": False, "error": None,
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": tot_in, "total_output_tokens": tot_out,
        "by_model": models,
    }


# ── Public ───────────────────────────────────────────────────────────────────
def provider_actuals(db: Session, *, start_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """Actual usage+cost per configured remote provider since `start_ts`
    (default: month start). Local providers are skipped (free; the caller shows
    them from the estimate). Each entry carries provider_id/type/name."""
    start = int(start_ts if start_ts is not None else _month_start_ts())
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    price = _price_map(db)
    out: List[Dict[str, Any]] = []
    for p in reg.list_providers():
        if p.is_local:
            continue
        meta = {"provider_id": p.id, "provider_type": p.provider_type, "provider_name": p.name}
        if p.provider_type not in TRUTH_CAPABLE:
            out.append({**meta, "available": False, "needs_key": False, "by_model": [],
                        "total_cost_usd": None,
                        "error": "No usage API for this provider — billing lives in its own console."})
            continue
        key = reg.get_admin_api_key(p.id) or reg.get_api_key(p.id)
        if not key:
            out.append({**meta, "available": False, "needs_key": True, "by_model": [],
                        "total_cost_usd": None, "error": "No Admin key set for this provider."})
            continue
        block = _openai_actuals(key, start, price) if p.provider_type == "openai" else _anthropic_actuals(key, start, price)
        out.append({**meta, **block})
    return out


def reconciled_spend(db: Session, *, start_ts: Optional[int] = None) -> Dict[str, Any]:
    """Merge the local-ledger estimate with provider-reported actuals into one
    per-provider view. Each provider block is tagged `source`:
      - "actual"     : from the provider's usage/cost API (truth)
      - "estimated"  : from the local token ledger (provider API unavailable)
      - "local-free" : local model, no cost
    Headline `total_cost_usd` = actual where available, else the estimate."""
    start = int(start_ts if start_ts is not None else _month_start_ts())
    from services.usage_service import UsageService
    est = UsageService(db).provider_breakdown(since_ts=start)  # by provider_type
    actuals = {a["provider_id"]: a for a in provider_actuals(db, start_ts=start)}

    from services.providers.registry_service import ProviderRegistryService
    providers = ProviderRegistryService(db).list_providers()

    blocks: List[Dict[str, Any]] = []
    actual_total = 0.0
    est_total = 0.0
    any_actual = False
    seen_types = set()

    for p in providers:
        seen_types.add(p.provider_type)
        a = actuals.get(p.id)
        e = est.get(p.provider_type) or {}
        if a and a.get("available"):
            cost = a.get("total_cost_usd")
            block = {
                "source": "actual",
                "cost_usd": cost,
                "tokens": int(a.get("total_input_tokens", 0)) + int(a.get("total_output_tokens", 0)),
                "by_model": [{**m, "source": "actual"} for m in a.get("by_model", [])],
            }
            any_actual = True
            if cost:
                actual_total += cost
        else:
            source = "local-free" if p.is_local else "estimated"
            cost = 0.0 if p.is_local else e.get("cost_usd")
            block = {
                "source": source,
                "cost_usd": cost,
                "tokens": int(e.get("total_tokens", 0)),
                "by_model": [{**m, "source": source} for m in e.get("by_model", [])],
            }
            if cost:
                est_total += cost
        blocks.append({
            "provider_id": p.id, "provider_type": p.provider_type, "provider_name": p.name,
            "is_local": p.is_local,
            "available": bool(a and a.get("available")),
            "needs_key": bool(a and a.get("needs_key")),
            "error": (a or {}).get("error"),
            **block,
        })

    # Estimate-only provider types with no matching configured provider (e.g. a
    # provider that was deleted but still has ledger history).
    for ptype, e in est.items():
        if ptype in seen_types:
            continue
        cost = e.get("cost_usd")
        if cost:
            est_total += cost
        blocks.append({
            "provider_id": None, "provider_type": ptype, "provider_name": ptype,
            "is_local": False, "available": False, "needs_key": False, "error": None,
            "source": "estimated", "cost_usd": cost, "tokens": int(e.get("total_tokens", 0)),
            "by_model": [{**m, "source": "estimated"} for m in e.get("by_model", [])],
        })

    return {
        "since_ts": start,
        "providers": blocks,
        "actual_cost_usd": round(actual_total, 4) if any_actual else None,
        "estimated_cost_usd": round(est_total, 4),
        "total_cost_usd": round(actual_total + est_total, 4),
    }
