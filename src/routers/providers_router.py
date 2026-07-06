"""
routers/providers_router.py

Admin CRUD for the provider/model registry + capability assignments.
Mutations require the Expert role (they manage credentials). API keys are
write-only — accepted on create/update, never returned.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from services.authz_service import assert_expert_role
from services.providers.registry_service import ProviderRegistryService
from schemas.provider import (
    CapabilityAssignmentRead,
    CapabilityAssignmentSet,
    ProviderCreate,
    ProviderModelCreate,
    ProviderModelRead,
    ProviderModelUpdate,
    ProviderRead,
    ProviderUpdate,
)

router = APIRouter(prefix="/admin/providers", tags=["Providers"])


def _svc(db: Session) -> ProviderRegistryService:
    return ProviderRegistryService(db)


@router.get("/presets")
def provider_presets(user=Depends(require_user)):
    """Known providers for the guided add-provider flow (label, base URL, where to
    get an API key, capabilities). Includes cloud Llama hosts (Ollama Cloud etc.)."""
    from services.providers.provider_presets import get_presets
    return get_presets()


# ── Providers ────────────────────────────────────────────────────────────────
@router.get("", response_model=List[ProviderRead])
def list_providers(db: Session = Depends(get_db), user=Depends(require_user)):
    return _svc(db).list_providers()


@router.post("", response_model=ProviderRead)
def create_provider(data: ProviderCreate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    return _svc(db).create_provider(data)


# NOTE: literal sub-paths (/models, /assignments) are declared BEFORE the
# parameterised /{provider_id} routes (at the bottom of this file), otherwise
# FastAPI matches e.g. PUT /assignments as {provider_id}="assignments" -> 422.


# ── Provider models ──────────────────────────────────────────────────────────
@router.get("/models", response_model=List[ProviderModelRead])
def list_models(
    provider_id: Optional[int] = Query(None),
    capability: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    return _svc(db).list_models(provider_id=provider_id, capability=capability)


@router.post("/models", response_model=ProviderModelRead)
def create_model(data: ProviderModelCreate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    return _svc(db).create_model(data)


@router.put("/models/{model_pk}", response_model=ProviderModelRead)
def update_model(model_pk: int, data: ProviderModelUpdate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    _VALID_CAPABILITIES = {"chat", "embeddings", "transcription", "tts", "realtime"}
    if data.capability is not None and data.capability not in _VALID_CAPABILITIES:
        raise HTTPException(status_code=400, detail=f"capability must be one of {sorted(_VALID_CAPABILITIES)}")
    try:
        out = _svc(db).update_model(model_pk, data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A model row with that (provider, model, capability) already exists")
    if out is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return out


@router.delete("/models/{model_pk}")
def delete_model(model_pk: int, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    if not _svc(db).delete_model(model_pk):
        raise HTTPException(status_code=404, detail="Model not found")
    return {"status": "deleted"}


@router.get("/{provider_id}/available-models")
def available_models(provider_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    """List the models this provider exposes (queries the provider's own API), so
    the user can add them without hand-typing ids."""
    assert_expert_role(user)
    svc = _svc(db)
    p = svc.get_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    from services.providers.model_discovery import discover_models
    return discover_models(
        provider_type=p.provider_type,
        base_url=p.base_url,
        api_key=svc.get_api_key(provider_id),
    )


# ── Per-model performance metrics (audit-derived rollup) ─────────────────────
@router.get("/model-metrics")
def model_metrics(
    since_hours: float = 24.0,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Per-model latency/error/timeout/validation rollup from the audit trail —
    response times (avg/p50/p95/max), slow calls vs the configured threshold,
    error + timeout rates, plan-validation failures/recoveries. Grouped by model
    id (which carries the version tag), so model-version regressions show up."""
    from services.model_metrics_service import ModelMetricsService
    return ModelMetricsService(db).summarize(since_hours=since_hours)


# ── Full-vs-light model evaluation (fixed task set, stored machine-readable) ──
@router.post("/model-eval")
async def start_model_eval(body: dict, user=Depends(require_user)):
    """Launch an evaluation run in the background: the fixed task set per
    (model × prompt mode), scored on completion/JSON validity/deviations and
    stored under BASE_DIR/eval/. Body: {"models": [...], "modes"?: ["full",
    "light"], "task_ids"?: [...]}. Local models run sequentially — expect a run
    to take minutes."""
    assert_expert_role(user)
    models = [m for m in (body.get("models") or []) if isinstance(m, str) and m.strip()]
    if not models:
        raise HTTPException(status_code=400, detail="models is required")
    from services.model_eval_service import run_eval
    import asyncio as _asyncio
    task = _asyncio.create_task(run_eval(
        models=models, modes=body.get("modes"), task_ids=body.get("task_ids"),
    ))
    # Keep a strong reference so the run isn't garbage-collected mid-flight.
    _EVAL_TASKS_RUNNING.add(task)
    task.add_done_callback(_EVAL_TASKS_RUNNING.discard)
    return {"status": "started", "note": "poll GET /admin/providers/model-eval for results"}


_EVAL_TASKS_RUNNING: set = set()


@router.get("/model-eval")
def list_model_evals(user=Depends(require_user)):
    from services.model_eval_service import list_runs
    return list_runs()


@router.get("/model-eval/{run_id}")
def get_model_eval(run_id: str, user=Depends(require_user)):
    from services.model_eval_service import get_run
    out = get_run(run_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Eval run not found")
    return out


# ── Capability assignments ───────────────────────────────────────────────────
@router.get("/assignments", response_model=List[CapabilityAssignmentRead])
def list_assignments(db: Session = Depends(get_db), user=Depends(require_user)):
    return _svc(db).list_assignments()


@router.put("/assignments", response_model=CapabilityAssignmentRead)
def set_assignment(data: CapabilityAssignmentSet, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        return _svc(db).set_assignment(data.slot, data.provider_model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Single provider by id (parameterised — declared LAST so literal paths win) ─
@router.put("/{provider_id}", response_model=ProviderRead)
def update_provider(provider_id: int, data: ProviderUpdate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    out = _svc(db).update_provider(provider_id, data)
    if out is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return out


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    if not _svc(db).delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"status": "deleted"}
