from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from component.logging import get_logger
from db.database import get_db
from schemas.workflow import (
    WorkflowCreate,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowTriggerRequest,
    WorkflowResumeRequest,
    WorkflowUpdate,
    WorkflowOperationRunRead,
)
from services.workflows.workflow_factory import WorkflowFactory
from services.workflows.workflow_run_service import WorkflowRunService
from services.workflows.workflow_service import WorkflowService
from authentication.dependencies import require_user
from services.authz_service import assert_expert_role

log = get_logger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])


def get_workflow_service(db: Session = Depends(get_db)) -> WorkflowService:
    log.debugx("WorkflowService dependency aanmaken")
    return WorkflowService(db)


def get_workflow_run_service(db: Session = Depends(get_db)) -> WorkflowRunService:
    log.debugx("WorkflowRunService dependency aanmaken")
    return WorkflowRunService(db)


@router.get("/model-override-issues")
def list_model_override_issues(db: Session = Depends(get_db)):
    """Workflow assistant operations whose pinned model override is no longer a
    registered/enabled chat model (e.g. after a model-id rename). Report-only."""
    from services.workflows.workflow_model_audit import find_stale_model_overrides
    issues = find_stale_model_overrides(db)
    return {"count": len(issues), "issues": issues}


@router.post("/generate")
async def generate_workflow_with_ai(body: dict, db: Session = Depends(get_db)):
    """Design a workflow from a description using the AI wizard model; creates it
    DISABLED and returns {id, name, steps} to open + review."""
    from services.workflows.workflow_ai import generate_and_create
    try:
        return await generate_and_create(db, body or {})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{workflow_id}/improve")
async def improve_workflow_with_ai(workflow_id: int, body: dict, db: Session = Depends(get_db)):
    """Improve an existing workflow with AI per the instruction and update it in
    place. Returns {id, name, steps}."""
    from services.workflows.workflow_ai import improve_and_update
    try:
        return await improve_and_update(db, workflow_id, (body or {}).get("instruction") or "")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("", response_model=WorkflowRead)
def create_workflow(data: WorkflowCreate, service: WorkflowService = Depends(get_workflow_service)):
    log.infox(
        "Workflow aanmaken gestart",
        name=getattr(data, "name", None),
        slug=getattr(data, "slug", None),
        enabled=getattr(data, "enabled", None),
    )
    result = service.create(data)
    log.infox(
        "Workflow aanmaken afgerond",
        workflow_id=getattr(result, "id", None),
        name=getattr(result, "name", None),
        slug=getattr(result, "slug", None),
        enabled=getattr(result, "enabled", None),
    )
    return result


@router.get("", response_model=List[WorkflowRead])
def list_workflows(
    skip: int = 0,
    limit: int = 100,
    include_disabled: bool = True,
    service: WorkflowService = Depends(get_workflow_service),
):
    log.infox(
        "Workflows ophalen gestart",
        skip=skip,
        limit=limit,
        include_disabled=include_disabled,
    )
    result = service.get_all(skip=skip, limit=limit, include_disabled=include_disabled)
    log.infox(
        "Workflows ophalen afgerond",
        skip=skip,
        limit=limit,
        include_disabled=include_disabled,
        count=len(result) if result is not None else None,
    )
    return result


@router.get("/{workflow_id}", response_model=WorkflowRead)
def get_workflow(workflow_id: int, service: WorkflowService = Depends(get_workflow_service)):
    log.infox(
        "Workflow ophalen gestart",
        workflow_id=workflow_id,
    )
    result = service.get_with_operations(workflow_id)
    log.infox(
        "Workflow ophalen afgerond",
        workflow_id=workflow_id,
        found=result is not None,
        operation_count=len(getattr(result, "operations", []) or []) if result is not None else None,
    )
    return result


@router.put("/{workflow_id}", response_model=WorkflowRead)
def update_workflow(
    workflow_id: int,
    data: WorkflowUpdate,
    service: WorkflowService = Depends(get_workflow_service),
):
    log.infox(
        "Workflow bijwerken gestart",
        workflow_id=workflow_id,
        name=getattr(data, "name", None),
        slug=getattr(data, "slug", None),
        enabled=getattr(data, "enabled", None),
    )
    result = service.update(workflow_id, data)
    log.infox(
        "Workflow bijwerken afgerond",
        workflow_id=workflow_id,
        result_id=getattr(result, "id", None),
        name=getattr(result, "name", None),
        slug=getattr(result, "slug", None),
        enabled=getattr(result, "enabled", None),
    )
    return result


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: int, service: WorkflowService = Depends(get_workflow_service)):
    log.infox(
        "Workflow verwijderen gestart",
        workflow_id=workflow_id,
    )
    result = service.delete(workflow_id)
    log.infox(
        "Workflow verwijderen afgerond",
        workflow_id=workflow_id,
    )
    return result


@router.post("/{workflow_id}/trigger", response_model=WorkflowRunRead)
def trigger_workflow(
    workflow_id: int,
    data: WorkflowTriggerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Manual trigger.

    This creates the run immediately. The BackgroundTasks executor is a dev-friendly
    default; for production, replace it with Celery/RQ/Arq/your worker queue.

    You must inject assistant_orchestrator below from your app container if you want
    automatic in-process execution. Otherwise remove the background task and let a
    worker execute queued runs.
    """
    log.infox(
        "Workflow handmatig triggeren gestart",
        workflow_id=workflow_id,
        has_input_payload=data.input_payload is not None,
        input_payload_keys=list(data.input_payload.keys()) if isinstance(data.input_payload, dict) else None,
    )
    workflow_service = WorkflowService(db)
    log.debugx(
        "WorkflowService aangemaakt voor handmatige trigger",
        workflow_id=workflow_id,
    )
    run_service = WorkflowRunService(db)
    log.debugx(
        "WorkflowRunService aangemaakt voor handmatige trigger",
        workflow_id=workflow_id,
    )
    factory = WorkflowFactory(workflow_service=workflow_service, workflow_run_service=run_service)
    log.debugx(
        "WorkflowFactory aangemaakt voor handmatige trigger",
        workflow_id=workflow_id,
    )
    run = factory.trigger_manual(workflow_id=workflow_id, input_payload=data.input_payload)
    log.infox(
        "Workflow handmatig triggeren afgerond",
        workflow_id=workflow_id,
        run_id=getattr(run, "id", None),
        run_status=getattr(run, "status", None),
    )
    return run


@router.get("/{workflow_id}/runs", response_model=List[WorkflowRunRead])
def list_workflow_runs(
    workflow_id: int,
    skip: int = 0,
    limit: int = 100,
    service: WorkflowRunService = Depends(get_workflow_run_service),
):
    log.infox(
        "Workflow runs ophalen gestart",
        workflow_id=workflow_id,
        skip=skip,
        limit=limit,
    )
    result = service.list_runs_for_workflow(workflow_id, skip=skip, limit=limit)
    log.infox(
        "Workflow runs ophalen afgerond",
        workflow_id=workflow_id,
        skip=skip,
        limit=limit,
        count=len(result) if result is not None else None,
    )
    return result


run_router = APIRouter(prefix="/workflow-runs", tags=["workflow-runs"])


@run_router.get("/{run_id}", response_model=WorkflowRunRead)
def get_workflow_run(run_id: int, service: WorkflowRunService = Depends(get_workflow_run_service)):
    log.infox(
        "Workflow run ophalen gestart",
        run_id=run_id,
    )
    result = service.get_run(run_id)
    log.infox(
        "Workflow run ophalen afgerond",
        run_id=run_id,
        found=result is not None,
        status=getattr(result, "status", None),
    )
    return result


@run_router.get("/{run_id}/operations", response_model=List[WorkflowOperationRunRead])
def get_workflow_run_operations(run_id: int, service: WorkflowRunService = Depends(get_workflow_run_service)):
    log.infox(
        "Workflow run operations ophalen gestart",
        run_id=run_id,
    )
    run = service.get_run_with_operations(run_id)
    result = run.operation_runs or []
    log.infox(
        "Workflow run operations ophalen afgerond",
        run_id=run_id,
        status=getattr(run, "status", None),
        operation_count=len(result),
    )
    return result


@run_router.get("/{run_id}/result")
def get_workflow_run_result(run_id: int, service: WorkflowRunService = Depends(get_workflow_run_service)):
    log.infox(
        "Workflow run resultaat ophalen gestart",
        run_id=run_id,
    )
    run = service.get_run(run_id)
    log.infox(
        "Workflow run resultaat ophalen afgerond",
        run_id=run_id,
        status=getattr(run, "status", None),
        has_result=getattr(run, "result_payload", None) is not None,
        has_error=bool(getattr(run, "error", None)),
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "result": run.result_payload,
        "error": run.error,
    }



@run_router.get("/{run_id}/pending")
def get_workflow_run_pending(
    run_id: int,
    service: WorkflowRunService = Depends(get_workflow_run_service),
):
    return service.get_pending(run_id)


@run_router.get("/{run_id}/operations/{operation_id}/pending")
def get_workflow_operation_pending(
    run_id: int,
    operation_id: int,
    service: WorkflowRunService = Depends(get_workflow_run_service),
):
    return service.get_pending(run_id, operation_id)


@run_router.post("/{run_id}/operations/{operation_id}/resume")
async def resume_workflow_operation(
    run_id: int,
    operation_id: int,
    data: WorkflowResumeRequest,
    service: WorkflowRunService = Depends(get_workflow_run_service),
    user=Depends(require_user),
):
    pending = service.get_pending(run_id, operation_id)
    pending_type = ((pending.get("pending") or {}).get("type") or "").strip()
    if pending_type == "workflow_tool_approval":
        assert_expert_role(user)
    if data.type == "approval" and data.approved is None:
        raise HTTPException(status_code=400, detail="approval resume requires approved boolean")
    if data.type == "user_input" and not (data.answer or "").strip():
        raise HTTPException(status_code=400, detail="user_input resume requires answer")
    return await service.resume_operation(
        run_id=run_id,
        operation_id=operation_id,
        payload=data.model_dump(exclude_none=True),
        resume_by=user,
    )

@run_router.post("/{run_id}/cancel", response_model=WorkflowRunRead)
def cancel_workflow_run(
    run_id: int,
    service: WorkflowRunService = Depends(get_workflow_run_service),
):
    log.infox(
        "Workflow run annuleren gestart",
        run_id=run_id,
    )
    result = service.cancel_run(run_id)
    log.infox(
        "Workflow run annuleren afgerond",
        run_id=run_id,
        status=getattr(result, "status", None),
    )
    return result