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


@internal_tool_registry.register(
    name="workflow__generate",
    title="Generate Workflow (AI draft)",
    description=(
        "Design and CREATE a new ND3X workflow from a plain-language description "
        "(steps may include assistant reasoning, tool calls, notifications "
        "(ui/email), http requests, set_variable). The draft is created DISABLED "
        "so the user reviews and enables it in the workflow builder. Returns the "
        "new workflow id, name and step count."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": ("What the workflow should do, step by step where "
                                 "possible — include the desired name, any email "
                                 "recipients, schedules and conditions."),
            },
        },
        "required": ["description"],
    },
    tags=["internal", "workflow"],
)
async def workflow_generate(args: Dict[str, Any]) -> Dict[str, Any]:
    description = str((args or {}).get("description") or "").strip()
    if not description:
        return {"status": "error", "error": "workflow__generate requires 'description'."}

    from db.database import SessionLocal
    from services.workflows.workflow_ai import generate_and_create

    with SessionLocal() as db:
        try:
            created = await generate_and_create(db, {"description": description})
        except Exception as exc:  # noqa: BLE001 — surface the reason to the agent
            return {"status": "error", "error": str(exc)}
    log.infox("workflow__generate: draft aangemaakt",
              workflow_id=created.get("id"), name=created.get("name"))
    return {
        "status": "success",
        "workflow_id": created.get("id"),
        "name": created.get("name"),
        "steps": created.get("steps"),
        "enabled": False,
        "note": ("Draft created DISABLED. Tell the user to review and enable it in "
                 "the Workflows builder; use workflow__describe to show its steps."),
    }


@internal_tool_registry.register(
    name="workflow__describe",
    title="Describe Workflow",
    description=(
        "Show a workflow's structure: its operations in order (type, name, config "
        "summary, dependencies) plus whether it is enabled. Use after "
        "workflow__generate to present the draft, or before modifying/running one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow": {"type": "string", "description": "Workflow name (or numeric id)."},
        },
        "required": ["workflow"],
    },
    tags=["internal", "workflow"],
)
async def workflow_describe(args: Dict[str, Any]) -> Dict[str, Any]:
    ref = str((args or {}).get("workflow") or "").strip()
    if not ref:
        return {"status": "error", "error": "workflow__describe requires 'workflow' (name or id)."}

    from db.database import SessionLocal
    from services.workflows.workflow_service import WorkflowService

    with SessionLocal() as db:
        svc = WorkflowService(db)
        wf = svc.get_by_id(int(ref)) if ref.isdigit() else None
        if wf is None:
            rows = svc.get_all(limit=1000)
            wf = next((w for w in rows if (w.name or "").lower() == ref.lower()), None) \
                or next((w for w in rows if ref.lower() in (w.name or "").lower()), None)
        if wf is None:
            return {"status": "error", "error": f"No workflow named '{ref}'. Use workflow__list."}
        ops = []
        for op in sorted(getattr(wf, "operations", []) or [], key=lambda o: o.position):
            cfg = op.config or {}
            hint = (cfg.get("question") or cfg.get("tool_name") or cfg.get("subject")
                    or cfg.get("message") or cfg.get("url") or cfg.get("variable") or "")
            ops.append({
                "position": op.position,
                "type": op.operation_type,
                "name": op.name,
                "summary": str(hint)[:200],
                "depends_on": list(op.depends_on or []),
                "config_keys": sorted(cfg.keys()),
            })
        return {
            "status": "success",
            "workflow_id": wf.id,
            "name": wf.name,
            "description": (wf.description or "")[:300],
            "enabled": bool(wf.is_enabled),
            "operations": ops,
        }
