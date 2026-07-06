"""Per-request token-usage accumulator (contextvar).

Providers push the actual usage from each LLM response; the ask/orchestration
boundary calls `reset()` before a turn and `drain()` after to read everything that
happened in that turn (router + planner + final + cognition, across providers) and
persist it to the token ledger. `add()` is a no-op unless `reset()` started a
collection in the current context, so stray calls never record.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger("svc.token_usage")

_events: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("token_usage_events", default=None)
# The orchestration stage/role for the call currently in flight. The OpenAI path
# passes `role` explicitly to add(); alternate-provider adapters don't know it, so
# the LLMRouter sets this around an alternate-provider dispatch and add() falls
# back to it — keeping the by-stage breakdown uniform across providers.
_role: ContextVar[Optional[str]] = ContextVar("token_usage_role", default=None)


def reset() -> None:
    """Begin collecting usage for the current request/turn."""
    _events.set([])


def set_role(role: Optional[str]):
    """Mark the current orchestration stage/role; returns a token for reset_role."""
    return _role.set(role)


def reset_role(token) -> None:
    _role.reset(token)


def add(
    *,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    model: Optional[str] = None,
    provider_type: Optional[str] = None,
    role: Optional[str] = None,
) -> None:
    events = _events.get()
    if events is None:
        return  # not collecting in this context — ignore
    in_tok = int(input_tokens or 0)
    out_tok = int(output_tokens or 0)
    effective_role = role if role is not None else _role.get()
    events.append(
        {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "model": model,
            "provider_type": provider_type,
            "role": effective_role,
        }
    )
    # Lightweight per-call line (debug — quiet unless debug logging is on).
    log.debugx(
        "token_usage:call",
        role=effective_role,
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=in_tok + out_tok,
    )


def drain() -> List[Dict[str, Any]]:
    events = _events.get()
    events = list(events) if events else []
    if events:
        # Per-turn by-stage rollup so token cost per stage (e.g. planner_continue)
        # is visible in the logs without enabling debug. One INFO line per turn.
        by_stage: Dict[str, Dict[str, int]] = {}
        total_in = total_out = 0
        for e in events:
            stage = str(e.get("role") or "unknown")
            agg = by_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            agg["calls"] += 1
            agg["input_tokens"] += int(e.get("input_tokens") or 0)
            agg["output_tokens"] += int(e.get("output_tokens") or 0)
            agg["total_tokens"] = agg["input_tokens"] + agg["output_tokens"]
            total_in += int(e.get("input_tokens") or 0)
            total_out += int(e.get("output_tokens") or 0)
        log.infox(
            "token_usage:by_stage",
            call_count=len(events),
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_tokens=total_in + total_out,
            by_stage=by_stage,
        )
    return events
