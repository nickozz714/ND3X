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
    name="workflow__create",
    title="Create Workflow",
    description=(
        "CREATE a new ND3X workflow from a step design YOU author (you have the "
        "conversation context — design the steps yourself, no second model). Linear "
        "chain: each operation runs after the previous. The workflow is created "
        "DISABLED so the user reviews and enables it in the builder. Returns the "
        "new workflow id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Workflow name."},
            "description": {"type": "string", "description": "What the workflow does (shown in the builder)."},
            "operations": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Ordered steps. Each: {type, name, ...type-specific fields}. Types: "
                    "assistant {question, skill_names?} · tool {tool_name, args?} · "
                    "notification {channel: ui|email|trace, subject, message, severity?, recipients?} · "
                    "http_request {method, url, headers?} · set_variable {variables} · "
                    "new_thread {variable?}. Reference earlier output with {{variables}}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string",
                                  "enum": ["assistant", "tool", "notification",
                                            "http_request", "set_variable", "new_thread"]},
                        "name": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["name", "operations"],
    },
    tags=["internal", "workflow"],
)
async def workflow_create(args: Dict[str, Any]) -> Dict[str, Any]:
    name = str((args or {}).get("name") or "").strip()
    steps = (args or {}).get("operations")
    if not name:
        return {"status": "error", "error": "workflow__create requires 'name'."}
    if not isinstance(steps, list) or not steps:
        return {"status": "error", "error": "workflow__create requires a non-empty 'operations' list."}

    from db.database import SessionLocal
    from services.workflows.workflow_ai import _op_config, _resolve_agent_id
    from schemas.workflow import WorkflowCreate, WorkflowOperationCreate
    from services.workflows.workflow_service import WorkflowService

    allowed = {"assistant", "tool", "notification", "http_request", "set_variable", "new_thread"}
    with SessionLocal() as db:
        agent_id = _resolve_agent_id(db)
        ops = []
        prev_pos = None
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                return {"status": "error", "error": f"operation {i + 1} must be an object."}
            t = str(step.get("type") or "").strip()
            if t not in allowed:
                return {"status": "error",
                        "error": f"operation {i + 1}: unknown type '{t}' (allowed: {sorted(allowed)})."}
            config = _op_config({**step, "type": t})
            # Fields _op_config doesn't map but the executor honours (e.g.
            # notification recipients) ride along via an explicit config dict.
            extra = step.get("config")
            if isinstance(extra, dict):
                config = {**config, **extra}
            if t == "notification" and isinstance(step.get("recipients"), list):
                config["recipients"] = [str(r) for r in step["recipients"] if str(r or "").strip()]
            position = (i + 1) * 100
            ops.append(WorkflowOperationCreate(
                name=(step.get("name") or f"Step {i + 1}").strip(),
                operation_type=t,
                operation_ref_id=agent_id if t == "assistant" else 0,
                config=config,
                depends_on=[prev_pos] if prev_pos is not None else [],
                position=position,
            ))
            prev_pos = position

        svc = WorkflowService(db)
        existing = {w.name for w in svc.get_all(limit=1000)}
        final = name
        n = 2
        while final in existing:
            final = f"{name} ({n})"; n += 1
        try:
            created = svc.create(WorkflowCreate(
                name=final, description=(args or {}).get("description"),
                is_enabled=False,  # review before enabling
                operations=ops,
            ))
        except Exception as exc:  # noqa: BLE001 — surface the reason to the agent
            return {"status": "error", "error": str(exc)}
    log.infox("workflow__create: draft aangemaakt", workflow_id=created.id, name=final, steps=len(ops))
    return {
        "status": "success",
        "workflow_id": created.id,
        "name": final,
        "steps": len(ops),
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
