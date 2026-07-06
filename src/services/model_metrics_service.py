"""
services/model_metrics_service.py

Per-model performance/timeout monitoring (TODO 1.4), aggregated from the audit
trail — no separate write path. The pipeline already records, per planner step:

- planner_call_end        (model, duration_s, output_chars, empty_output)
- planner_call_error      (model, duration_s, error, message) — incl. timeouts
- plan_validation_failed  (model, problems, retries_used)
- tool_block_recovering   (blocked-tool recovery hop)

This service rolls those up per model id (the id carries the version tag, e.g.
``qwen2.5:14b``), so regressions between model versions are directly visible.
Slow steps are counted against ``settings.MODEL_SLOW_STEP_WARN_S`` — the same
threshold the pipeline uses to WARN-flag a slow planner call in the audit.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.config import settings
from component.logging import get_logger
from models.audit import AuditTraceEvent

log = get_logger(__name__)

_EVENT_TYPES = (
    "planner_call_end",
    "planner_call_error",
    "plan_validation_failed",
    "tool_block_recovering",
)


def slow_step_threshold_s() -> float:
    try:
        return float(getattr(settings, "MODEL_SLOW_STEP_WARN_S", 90) or 90)
    except Exception:  # noqa: BLE001
        return 90.0


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, int(round(pct * (len(sorted_values) - 1)))))
    return sorted_values[idx]


class ModelMetricsService:
    def __init__(self, db: Session):
        self.db = db

    def summarize(self, *, since_hours: float = 24.0, max_events: int = 20000) -> Dict[str, Any]:
        """Per-model rollup over the audit window: call counts, latency
        (avg/p50/p95/max), slow calls, errors + timeouts, empty outputs,
        validation failures/recoveries, blocked-tool recoveries."""
        cutoff = time.time() - since_hours * 3600
        rows = (
            self.db.query(AuditTraceEvent)
            .filter(AuditTraceEvent.type.in_(_EVENT_TYPES), AuditTraceEvent.ts >= cutoff)
            .order_by(AuditTraceEvent.ts.asc())
            .limit(max_events)
            .all()
        )
        threshold = slow_step_threshold_s()
        per_model: Dict[str, Dict[str, Any]] = {}

        def bucket(model: Optional[str]) -> Dict[str, Any]:
            key = (model or "(slot-routed)").strip() or "(slot-routed)"
            b = per_model.get(key)
            if b is None:
                b = per_model[key] = {
                    "model": key,
                    "calls": 0,
                    "durations": [],
                    "slow_calls": 0,
                    "empty_outputs": 0,
                    "errors": 0,
                    "timeouts": 0,
                    "validation_failures": 0,
                    "validation_exhausted": 0,
                    "tool_block_recoveries": 0,
                }
            return b

        for row in rows:
            try:
                data = json.loads(row.data_json or "{}")
            except Exception:  # noqa: BLE001 — skip a garbled audit row
                continue
            model = data.get("model")
            b = bucket(model)
            if row.type == "planner_call_end":
                b["calls"] += 1
                dur = data.get("duration_s")
                if isinstance(dur, (int, float)):
                    b["durations"].append(float(dur))
                    if float(dur) >= threshold:
                        b["slow_calls"] += 1
                if data.get("empty_output"):
                    b["empty_outputs"] += 1
            elif row.type == "planner_call_error":
                b["errors"] += 1
                err = f"{data.get('error') or ''} {data.get('message') or ''}".lower()
                if "timeout" in err:
                    b["timeouts"] += 1
            elif row.type == "plan_validation_failed":
                b["validation_failures"] += 1
                if int(data.get("retries_used") or 0) >= 2:
                    b["validation_exhausted"] += 1
            elif row.type == "tool_block_recovering":
                b["tool_block_recoveries"] += 1

        models: List[Dict[str, Any]] = []
        for b in per_model.values():
            durations = sorted(b.pop("durations"))
            calls = b["calls"]
            attempts = calls + b["errors"]
            recovered = b["validation_failures"] - b["validation_exhausted"]
            models.append({
                **b,
                "avg_s": round(sum(durations) / len(durations), 2) if durations else None,
                "p50_s": round(_percentile(durations, 0.50), 2) if durations else None,
                "p95_s": round(_percentile(durations, 0.95), 2) if durations else None,
                "max_s": round(durations[-1], 2) if durations else None,
                "error_rate": round(b["errors"] / attempts, 3) if attempts else 0.0,
                "timeout_rate": round(b["timeouts"] / attempts, 3) if attempts else 0.0,
                # A validation failure that was later corrected within the same
                # turn budget counts as a successful recovery.
                "validation_recovered": max(recovered, 0),
            })
        models.sort(key=lambda m: -(m["calls"] + m["errors"]))
        return {
            "since_hours": since_hours,
            "slow_step_threshold_s": threshold,
            "event_rows": len(rows),
            "models": models,
        }
