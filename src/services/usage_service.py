"""Token usage ledger + context-budget + monthly-budget logic.

Deterministic and provider-agnostic: records token usage per thread/turn, derives
how much of the active model's context window is left, aggregates usage for the
dashboard, and tracks a user-set monthly budget. No LLM calls.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from component.logging import get_logger
from models.token_usage import TokenUsage, UsageBudget

log = get_logger(__name__)

# Fraction of the context window at which we warn / trigger compaction.
NEAR_CONTEXT_RATIO = 0.85


def stage_of(role: Optional[str]) -> str:
    """Collapse a fine-grained role tag into a coarse orchestration stage.

    Roles are recorded per-turn with identifiers attached
    (`router:123`, `writer:assistant:5`, `cognition:planner`, `memory_decision:router`).
    For the by-stage breakdown we group by the leading segment so all turns of the
    same kind aggregate together; an empty/None role becomes `unknown`."""
    if not role:
        return "unknown"
    return str(role).split(":", 1)[0]


def _month_start_ts() -> float:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()


class UsageService:
    def __init__(self, db: Session):
        self.db = db

    # ── recording ─────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
        turn_id: Optional[int] = None,
        role: Optional[str] = None,
        provider_type: Optional[str] = None,
        model: Optional[str] = None,
        estimated: bool = True,
    ) -> TokenUsage:
        rec = TokenUsage(
            ts=time.time(),
            thread_id=thread_id,
            turn_id=turn_id,
            role=role,
            provider_type=provider_type,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            total_tokens=int(input_tokens or 0) + int(output_tokens or 0),
            estimated=bool(estimated),
        )
        self.db.add(rec)
        self.db.commit()
        return rec

    # ── per-conversation context budget ────────────────────────────────────────
    def thread_usage(self, thread_id: str, context_window: Optional[int] = None) -> Dict[str, Any]:
        row = (
            self.db.query(
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            )
            .filter(TokenUsage.thread_id == thread_id)
            .one()
        )
        used_in, used_out, used_total = int(row[0]), int(row[1]), int(row[2])
        # Current context occupancy ≈ the most recent call's input size (it already
        # includes the full prior context). Summing every stage's input would
        # massively double-count, so context_left is based on the latest input, not
        # the cumulative sum (which is the cost/budget figure).
        latest = (
            self.db.query(TokenUsage.input_tokens)
            .filter(TokenUsage.thread_id == thread_id)
            .order_by(TokenUsage.ts.desc(), TokenUsage.id.desc())
            .first()
        )
        context_tokens = int(latest[0]) if latest and latest[0] else 0
        window = int(context_window) if context_window else None
        left = max(window - context_tokens, 0) if window else None
        ratio = (context_tokens / window) if window else None

        # Estimated $ spend for this conversation, summed per model from the price
        # map. None when no model in the thread has pricing (e.g. local Ollama).
        price = self._price_map()
        cost = 0.0
        any_cost = False
        for model, i, o in (
            self.db.query(
                TokenUsage.model,
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
            ).filter(TokenUsage.thread_id == thread_id).group_by(TokenUsage.model).all()
        ):
            c = self._cost_of(i, o, price.get(model))
            if c is not None:
                any_cost = True
                cost += c

        return {
            "thread_id": thread_id,
            "input_tokens": used_in,
            "output_tokens": used_out,
            "used_tokens": used_total,          # cumulative tokens (cost/budget)
            "cost_usd": round(cost, 4) if any_cost else None,  # estimated $ this conversation
            "context_tokens": context_tokens,   # current context-window occupancy
            "context_window": window,
            "tokens_left": left,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "near_limit": bool(ratio is not None and ratio >= NEAR_CONTEXT_RATIO),
        }

    # ── per-session (per-thread) aggregation ────────────────────────────────────
    def sessions(self, since_ts: Optional[float] = None) -> list[Dict[str, Any]]:
        """Usage grouped per conversation/thread: total tokens, a by-stage (role)
        breakdown, and when the thread was last active. Ordered most-recent first."""
        q = self.db.query(TokenUsage)
        if since_ts is not None:
            q = q.filter(TokenUsage.ts >= since_ts)

        totals = (
            q.with_entities(
                TokenUsage.thread_id,
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                func.max(TokenUsage.ts),
            )
            .group_by(TokenUsage.thread_id)
            .all()
        )
        stage_rows = (
            q.with_entities(
                TokenUsage.thread_id,
                TokenUsage.role,
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            )
            .group_by(TokenUsage.thread_id, TokenUsage.role)
            .all()
        )
        by_stage: Dict[str, Dict[str, int]] = {}
        for thread_id, role, tok in stage_rows:
            stage = stage_of(role)
            bucket = by_stage.setdefault(thread_id, {})
            bucket[stage] = bucket.get(stage, 0) + int(tok)

        # Friendly conversation titles (raw thread_id is opaque to users).
        titles = self._thread_titles([t[0] for t in totals])
        sessions = [
            {
                "thread_id": thread_id,
                "title": titles.get(thread_id),
                "total_tokens": int(total),
                "by_stage": by_stage.get(thread_id, {}),
                "last_used": float(last_used) if last_used is not None else None,
            }
            for thread_id, total, last_used in totals
        ]
        sessions.sort(key=lambda s: (s["last_used"] or 0.0), reverse=True)
        return sessions

    def _thread_titles(self, thread_ids: list[str]) -> Dict[str, Optional[str]]:
        """Map thread_id -> title (best-effort; empty when the threads table is absent)."""
        ids = [t for t in thread_ids if t]
        if not ids:
            return {}
        try:
            from models.assistant_thread import AssistantThreadModel
            rows = (
                self.db.query(AssistantThreadModel.id, AssistantThreadModel.title)
                .filter(AssistantThreadModel.id.in_(ids))
                .all()
            )
            return {tid: (title or None) for tid, title in rows}
        except Exception:  # noqa: BLE001 — titles are cosmetic
            return {}

    # ── per-provider estimate (from the local ledger) ───────────────────────────
    def provider_breakdown(self, since_ts: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """Per provider_type: tokens + estimated cost + per-model breakdown, from the
        ledger. Keyed by provider_type. Used as the estimate side of reconciliation."""
        q = self.db.query(TokenUsage)
        if since_ts is not None:
            q = q.filter(TokenUsage.ts >= since_ts)
        price = self._price_map()
        out: Dict[str, Dict[str, Any]] = {}
        for ptype, model, i, o, t in (
            q.with_entities(
                TokenUsage.provider_type, TokenUsage.model,
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            ).group_by(TokenUsage.provider_type, TokenUsage.model).all()
        ):
            key = ptype or "unknown"
            blk = out.setdefault(key, {
                "provider_type": key, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "cost_usd": 0.0, "_any_cost": False, "by_model": [],
            })
            c = self._cost_of(i, o, price.get(model))
            blk["input_tokens"] += int(i)
            blk["output_tokens"] += int(o)
            blk["total_tokens"] += int(t)
            if c is not None:
                blk["cost_usd"] += c
                blk["_any_cost"] = True
            blk["by_model"].append({
                "model": model or "unknown", "input_tokens": int(i), "output_tokens": int(o),
                "total_tokens": int(t), "cost_usd": round(c, 4) if c is not None else None,
            })
        for blk in out.values():
            blk["cost_usd"] = round(blk["cost_usd"], 4) if blk.pop("_any_cost") else None
            blk["by_model"].sort(key=lambda x: -x["total_tokens"])
        return out

    # ── per-conversation breakdown (estimate; provider APIs are org-level) ───────
    def thread_breakdown(self, thread_id: str) -> Dict[str, Any]:
        price = self._price_map()
        rows = (
            self.db.query(
                TokenUsage.provider_type, TokenUsage.model,
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            )
            .filter(TokenUsage.thread_id == thread_id)
            .group_by(TokenUsage.provider_type, TokenUsage.model)
            .all()
        )
        by_provider: Dict[str, Dict[str, Any]] = {}
        total_tokens = 0
        total_cost = 0.0
        any_cost = False
        for ptype, model, i, o, t in rows:
            key = ptype or "unknown"
            blk = by_provider.setdefault(key, {
                "provider_type": key, "total_tokens": 0, "cost_usd": 0.0, "_any": False, "by_model": [],
            })
            c = self._cost_of(i, o, price.get(model))
            blk["total_tokens"] += int(t)
            total_tokens += int(t)
            if c is not None:
                blk["cost_usd"] += c
                blk["_any"] = True
                total_cost += c
                any_cost = True
            blk["by_model"].append({
                "model": model or "unknown", "input_tokens": int(i), "output_tokens": int(o),
                "total_tokens": int(t), "cost_usd": round(c, 4) if c is not None else None,
            })
        providers = []
        for blk in by_provider.values():
            blk["cost_usd"] = round(blk["cost_usd"], 4) if blk.pop("_any") else None
            blk["by_model"].sort(key=lambda x: -x["total_tokens"])
            providers.append(blk)
        providers.sort(key=lambda x: -x["total_tokens"])
        return {
            "thread_id": thread_id,
            "title": self._thread_titles([thread_id]).get(thread_id),
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(total_cost, 4) if any_cost else None,
            "by_provider": providers,
        }

    # ── global dashboard aggregation ───────────────────────────────────────────
    def _price_map(self) -> Dict[str, tuple]:
        """model_id -> (price_in, price_out) in USD per 1M tokens, for models that
        have pricing set (filled by enriched discovery)."""
        out: Dict[str, tuple] = {}
        try:
            from models.provider import ProviderModel
            for m in self.db.query(ProviderModel).all():
                if m.price_in is not None or m.price_out is not None:
                    out[m.model_id] = (m.price_in, m.price_out)
        except Exception:  # noqa: BLE001 — pricing is best-effort (table may be absent)
            return {}
        return out

    @staticmethod
    def _cost_of(input_tokens: int, output_tokens: int, price: Optional[tuple]) -> Optional[float]:
        if not price:
            return None
        pin, pout = price
        return (int(input_tokens) / 1_000_000) * (pin or 0.0) + (int(output_tokens) / 1_000_000) * (pout or 0.0)

    def cost_since(self, since_ts: Optional[float] = None) -> float:
        """Total estimated USD cost since since_ts, summed per model from prices."""
        q = self.db.query(
            TokenUsage.model,
            func.coalesce(func.sum(TokenUsage.input_tokens), 0),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0),
        )
        if since_ts is not None:
            q = q.filter(TokenUsage.ts >= since_ts)
        price = self._price_map()
        total = 0.0
        for model, i, o in q.group_by(TokenUsage.model).all():
            c = self._cost_of(i, o, price.get(model))
            if c:
                total += c
        return round(total, 4)

    def summary(self, since_ts: Optional[float] = None) -> Dict[str, Any]:
        q = self.db.query(TokenUsage)
        if since_ts is not None:
            q = q.filter(TokenUsage.ts >= since_ts)

        price = self._price_map()

        def _grouped(col):
            rows = (
                q.with_entities(col, func.coalesce(func.sum(TokenUsage.total_tokens), 0))
                .group_by(col)
                .all()
            )
            return [{"key": (k or "unknown"), "total_tokens": int(v)} for k, v in rows]

        # By stage: group by raw role in SQL, then collapse fine-grained role tags
        # (router:123, writer:asst:5, ...) into coarse stages in Python.
        stage_totals: Dict[str, int] = {}
        for role, tok in (
            q.with_entities(TokenUsage.role, func.coalesce(func.sum(TokenUsage.total_tokens), 0))
            .group_by(TokenUsage.role)
            .all()
        ):
            stage = stage_of(role)
            stage_totals[stage] = stage_totals.get(stage, 0) + int(tok)
        by_stage = sorted(
            ({"key": k, "total_tokens": v} for k, v in stage_totals.items()),
            key=lambda x: -x["total_tokens"],
        )

        total = int(q.with_entities(func.coalesce(func.sum(TokenUsage.total_tokens), 0)).scalar() or 0)

        # Per-model token + cost breakdown (cost from the price map; None when the
        # model has no pricing, e.g. local Ollama).
        by_model = []
        total_cost = 0.0
        any_cost = False
        for model, i, o, t in (
            q.with_entities(
                TokenUsage.model,
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            ).group_by(TokenUsage.model).all()
        ):
            c = self._cost_of(i, o, price.get(model))
            if c is not None:
                any_cost = True
                total_cost += c
            by_model.append({
                "key": model or "unknown",
                "total_tokens": int(t),
                "input_tokens": int(i),
                "output_tokens": int(o),
                "cost_usd": round(c, 4) if c is not None else None,
            })
        by_model.sort(key=lambda x: -x["total_tokens"])

        return {
            "since_ts": since_ts,
            "total_tokens": total,
            "estimated_cost_usd": round(total_cost, 4) if any_cost else None,
            "by_model": by_model,
            "by_provider": sorted(_grouped(TokenUsage.provider_type), key=lambda x: -x["total_tokens"]),
            "by_stage": by_stage,
        }

    # ── monthly budget ─────────────────────────────────────────────────────────
    def _budget_row(self) -> Optional[UsageBudget]:
        return self.db.query(UsageBudget).order_by(UsageBudget.id.asc()).first()

    def set_budget(self, *, monthly_token_budget: Optional[int], monthly_cost_budget_usd: Optional[float]) -> UsageBudget:
        row = self._budget_row()
        if row is None:
            row = UsageBudget()
            self.db.add(row)
        row.monthly_token_budget = monthly_token_budget
        row.monthly_cost_budget_usd = monthly_cost_budget_usd
        row.updated_at = time.time()
        self.db.commit()
        return row

    def budget_status(self) -> Dict[str, Any]:
        row = self._budget_row()
        month_start = _month_start_ts()
        used = int(
            self.db.query(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
            .filter(TokenUsage.ts >= month_start)
            .scalar()
            or 0
        )
        token_budget = row.monthly_token_budget if row else None
        left = max(token_budget - used, 0) if token_budget else None
        ratio = (used / token_budget) if token_budget else None

        used_cost = self.cost_since(month_start)
        cost_budget = row.monthly_cost_budget_usd if row else None
        cost_ratio = (used_cost / cost_budget) if cost_budget else None
        return {
            "monthly_token_budget": token_budget,
            "monthly_cost_budget_usd": cost_budget,
            "used_tokens_this_month": used,
            "tokens_left_this_month": left,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "over_budget": bool(ratio is not None and ratio >= 1.0),
            "used_cost_usd_this_month": used_cost,
            "cost_left_this_month": round(max(cost_budget - used_cost, 0.0), 4) if cost_budget else None,
            "cost_ratio": round(cost_ratio, 4) if cost_ratio is not None else None,
            "over_cost_budget": bool(cost_ratio is not None and cost_ratio >= 1.0),
        }

    # ── platform statistics (dashboard) ──────────────────────────────────────
    def platform_stats(self, days: int = 30) -> Dict[str, Any]:
        """Daily platform statistics aggregated from the EXISTING ledgers — token
        usage per day (stacked by provider, with estimated cost), messages per day,
        and workflow-run reliability. No new tracking; everything is derived from
        indexed columns (TokenUsage.ts, thread-message created_at, workflow_run)."""
        from datetime import timedelta

        from models.assistant_thread import AssistantThreadMessageModel
        from models.workflow import WorkflowRun

        days = max(1, min(int(days or 30), 365))
        now = datetime.now(timezone.utc)
        since_dt = now - timedelta(days=days)
        since_ts = since_dt.timestamp()
        since_iso = since_dt.strftime("%Y-%m-%d")
        price = self._price_map()

        # Tokens & estimated cost per day (grouped per model so pricing applies,
        # folded to per-day rows with a per-provider split for the stacked chart).
        day = func.date(TokenUsage.ts, "unixepoch")
        daily: Dict[str, Dict[str, Any]] = {}
        for d, ptype, model, i, o in (
            self.db.query(
                day.label("d"), TokenUsage.provider_type, TokenUsage.model,
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
            )
            .filter(TokenUsage.ts >= since_ts)
            .group_by("d", TokenUsage.provider_type, TokenUsage.model)
            .order_by("d")
            .all()
        ):
            row = daily.setdefault(str(d), {
                "date": str(d), "input_tokens": 0, "output_tokens": 0,
                "tokens": 0, "est_cost_usd": 0.0, "by_provider": {},
            })
            i, o = int(i or 0), int(o or 0)
            row["input_tokens"] += i
            row["output_tokens"] += o
            row["tokens"] += i + o
            p = ptype or "unknown"
            row["by_provider"][p] = row["by_provider"].get(p, 0) + i + o
            c = self._cost_of(i, o, price.get(model))
            if c:
                row["est_cost_usd"] = round(row["est_cost_usd"] + c, 6)

        # Messages per day (user vs assistant) + active threads in the window.
        mday = func.substr(AssistantThreadMessageModel.created_at, 1, 10)
        messages: Dict[str, Dict[str, Any]] = {}
        for d, role, n in (
            self.db.query(mday.label("d"), AssistantThreadMessageModel.role, func.count())
            .filter(AssistantThreadMessageModel.created_at >= since_iso)
            .group_by("d", AssistantThreadMessageModel.role)
            .order_by("d")
            .all()
        ):
            row = messages.setdefault(str(d), {"date": str(d), "user": 0, "assistant": 0})
            key = "user" if str(role) == "user" else "assistant"
            row[key] += int(n or 0)
        active_threads = int(
            self.db.query(func.count(func.distinct(AssistantThreadMessageModel.thread_id)))
            .filter(AssistantThreadMessageModel.created_at >= since_iso)
            .scalar() or 0
        )

        # Workflow runs per day + reliability.
        ok_states = ("success",)
        bad_states = ("failed", "error", "cancelled")
        wday = func.date(WorkflowRun.created_at)
        runs: Dict[str, Dict[str, Any]] = {}
        total_ok = total_bad = total_runs = 0
        for d, status, n in (
            self.db.query(wday.label("d"), WorkflowRun.status, func.count())
            .filter(WorkflowRun.created_at >= since_dt.replace(tzinfo=None))
            .group_by("d", WorkflowRun.status)
            .order_by("d")
            .all()
        ):
            row = runs.setdefault(str(d), {"date": str(d), "success": 0, "failed": 0, "other": 0})
            n = int(n or 0)
            total_runs += n
            if status in ok_states:
                row["success"] += n
                total_ok += n
            elif status in bad_states:
                row["failed"] += n
                total_bad += n
            else:
                row["other"] += n
        finished = total_ok + total_bad
        avg_duration_s = self.db.query(
            func.avg((func.julianday(WorkflowRun.finished_at) - func.julianday(WorkflowRun.started_at)) * 86400.0)
        ).filter(
            WorkflowRun.created_at >= since_dt.replace(tzinfo=None),
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.started_at.isnot(None),
        ).scalar()

        # Headline KPIs for the window.
        window_tokens = sum(r["tokens"] for r in daily.values())
        window_cost = round(sum(r["est_cost_usd"] for r in daily.values()), 4)
        window_messages = sum(r["user"] + r["assistant"] for r in messages.values())

        return {
            "days": days,
            "kpis": {
                "tokens": window_tokens,
                "est_cost_usd": window_cost,
                "messages": window_messages,
                "active_threads": active_threads,
                "workflow_runs": total_runs,
                "workflow_success_rate": round(total_ok / finished, 4) if finished else None,
                "workflow_avg_duration_s": round(float(avg_duration_s), 1) if avg_duration_s else None,
            },
            "daily_tokens": sorted(daily.values(), key=lambda r: r["date"]),
            "daily_messages": sorted(messages.values(), key=lambda r: r["date"]),
            "daily_workflow_runs": sorted(runs.values(), key=lambda r: r["date"]),
        }
