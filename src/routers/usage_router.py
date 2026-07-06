from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from services.providers.registry_service import ProviderRegistryService
from services.usage_service import UsageService, _month_start_ts

router = APIRouter(prefix="/main/usage", tags=["usage"], dependencies=[Depends(require_user)])


def _context_window_for(db: Session, model: Optional[str]) -> Optional[int]:
    """Context window for the relevant chat model: an explicit `model`, else the
    model assigned to the `chat.planner` slot (the agent that writes the
    answers in single-agent mode)."""
    reg = ProviderRegistryService(db)
    target = model
    if not target:
        try:
            resolved = reg.resolve_slot("chat.planner")
            target = resolved.model_id if resolved else None
        except Exception:  # noqa: BLE001
            target = None
    if not target:
        return None
    try:
        for m in reg.list_models(capability="chat"):
            if m.model_id == target and m.context_window:
                return int(m.context_window)
    except Exception:  # noqa: BLE001
        return None
    return None


@router.get("/thread/{thread_id}")
def thread_usage(thread_id: str, model: Optional[str] = None, db: Session = Depends(get_db)):
    window = _context_window_for(db, model)
    return UsageService(db).thread_usage(thread_id, context_window=window)


@router.get("/sessions")
def usage_sessions(period: str = "month", db: Session = Depends(get_db)):
    since = _month_start_ts() if period == "month" else None
    return UsageService(db).sessions(since_ts=since)


@router.get("/thread/{thread_id}/breakdown")
def thread_breakdown(thread_id: str, db: Session = Depends(get_db)):
    """Per-provider + per-model token/cost breakdown for one conversation, from
    the local ledger (estimate — provider usage APIs are org-level, not per-thread)."""
    return UsageService(db).thread_breakdown(thread_id)


@router.get("/summary")
def usage_summary(period: str = "month", db: Session = Depends(get_db)):
    since = _month_start_ts() if period == "month" else None
    return UsageService(db).summary(since_ts=since)


@router.get("/budget")
def get_budget(db: Session = Depends(get_db)):
    return UsageService(db).budget_status()


class BudgetIn(BaseModel):
    monthly_token_budget: Optional[int] = None
    monthly_cost_budget_usd: Optional[float] = None


@router.put("/budget")
def set_budget(body: BudgetIn, db: Session = Depends(get_db)):
    svc = UsageService(db)
    svc.set_budget(
        monthly_token_budget=body.monthly_token_budget,
        monthly_cost_budget_usd=body.monthly_cost_budget_usd,
    )
    return svc.budget_status()


@router.get("/reconciled")
def reconciled_spend(period: str = "month", db: Session = Depends(get_db)):
    """Per-provider spend this period, merging provider-reported ACTUALS (truth,
    tagged source="actual") with the local-ledger estimate (source="estimated"/
    "local-free") where a provider has no usage API. Admin keys live per provider
    in the AI Models tab. Headline total = actual where available, else estimate."""
    from services.provider_usage_service import reconciled_spend as _reconciled
    start = _month_start_ts() if period == "month" else None
    return _reconciled(db, start_ts=start)


@router.post("/thread/{thread_id}/compact")
async def compact_thread(thread_id: str, db: Session = Depends(get_db)):
    """Manually compact a conversation: summarise it + reset the context chain so
    the next turn starts smaller. The full thread is preserved."""
    from services.compaction_service import CompactionService
    from services.assistants.ask_job_callbacks import openai
    summary = await CompactionService(db).compact(thread_id, openai)
    return {"thread_id": thread_id, "compacted": bool(summary), "summary": summary}
