"""
services/builtin/tools/workflow_tools.py

Builtin tools that let the agent LIST and RUN workflows. This is what makes a
skill able to "be a workflow": a skill bundles workflow__run + instructions to
kick off a specific workflow, so selecting the skill launches that workflow.

Registered on import (imported in ask_job_callbacks.py).
"""
from __future__ import annotations

from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


@internal_tool_registry.register(
    name="workflow__list",
    title="List Workflows",
    description="List the available workflows (id, name, description) so you can pick one to run.",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "workflow"],
)
async def workflow_list(_args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from services.workflows.workflow_service import WorkflowService
    with SessionLocal() as db:
        rows = WorkflowService(db).get_all(limit=1000, include_disabled=False)
        return {
            "status": "success",
            "workflows": [
                {"id": w.id, "name": w.name, "description": (w.description or "")[:200], "enabled": w.is_enabled}
                for w in rows
            ],
        }


@internal_tool_registry.register(
    name="workflow__run",
    title="Run Workflow",
    description=(
        "Start a workflow by name or id, optionally with an input payload. The "
        "workflow runs in the background; this returns its run_id so it can be "
        "followed in the Workflows tile. Use workflow__list first if unsure of the name."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow": {"type": "string", "description": "Workflow name (or numeric id) to run."},
            "input": {"type": "object", "description": "Optional input payload passed to the workflow."},
        },
        "required": ["workflow"],
    },
    tags=["internal", "workflow"],
)
async def workflow_run(args: Dict[str, Any]) -> Dict[str, Any]:
    ref = str((args or {}).get("workflow") or "").strip()
    if not ref:
        return {"status": "error", "error": "workflow__run requires 'workflow' (name or id)."}
    input_payload = args.get("input") if isinstance(args.get("input"), dict) else {}

    from db.database import SessionLocal
    from services.workflows.workflow_service import WorkflowService
    from services.workflows.workflow_run_service import WorkflowRunService
    from services.workflows.workflow_factory import WorkflowFactory

    with SessionLocal() as db:
        svc = WorkflowService(db)
        wf = None
        if ref.isdigit():
            wf = svc.get_by_id(int(ref))
        if wf is None:
            rows = svc.get_all(limit=1000)
            wf = next((w for w in rows if (w.name or "").lower() == ref.lower()), None) \
                or next((w for w in rows if ref.lower() in (w.name or "").lower()), None)
        if wf is None:
            return {"status": "error", "error": f"No workflow named '{ref}'. Use workflow__list to see the options."}
        if not wf.is_enabled:
            return {"status": "error", "error": f"Workflow '{wf.name}' is disabled."}

        factory = WorkflowFactory(workflow_service=svc, workflow_run_service=WorkflowRunService(db))
        run = factory.trigger_manual(workflow_id=wf.id, input_payload=input_payload)
        run_id = getattr(run, "id", None)
        log.infox("workflow__run gestart", workflow=wf.name, workflow_id=wf.id, run_id=run_id)
        return {
            "status": "success",
            "workflow": wf.name,
            "workflow_id": wf.id,
            "run_id": run_id,
            "note": f"Workflow '{wf.name}' started (run #{run_id}). It runs in the background; follow it in the Workflows tile.",
        }
