"""
services/workflows/workflow_ai.py

"Generate with AI" for workflows: from a short description an LLM designs a
SEQUENTIAL workflow (a linear chain of steps) using a constrained, reliably
generatable set of operation types. The draft is created disabled so the user
reviews/edits it in the builder before enabling.

Kept deliberately linear (each step depends on the previous) — branching /
loops are left to manual editing, so the generated graph is always valid.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

# Operation types the generator may emit. Kept to self-contained + assistant so
# the produced graph is always valid without cross-references beyond the linear
# depends_on chain we add ourselves.
_ALLOWED = ("assistant", "tool", "set_variable", "new_thread", "notification", "http_request")

_INSTRUCTIONS = (
    "You design automation WORKFLOWS as a SEQUENTIAL list of steps. Given the user's "
    "description, output ONE JSON object:\n"
    "- name: short workflow name\n"
    "- description: one sentence\n"
    "- operations: an ordered array of steps. Each step: {name, type, ...type-fields}. "
    "Allowed types and their fields:\n"
    "  • assistant  → { question: '<what the agent should do this step, may reference "
    "${workflow_input.x} or a prior step>', skill_names: [] }  (the platform's one agent runs it)\n"
    "  • tool       → { tool_name: '<builtin tool>', args: { } }  (e.g. web_search, text__ingest)\n"
    "  • set_variable → { variables: { name: value } }\n"
    "  • new_thread → { variable: 'thread' }  (create a shared conversation; put it EARLY so later "
    "assistant steps can share one thread)\n"
    "  • notification → { channel: 'ui', severity: 'info', subject: '', message: '' }\n"
    "  • http_request → { method: 'GET', url: '', headers: {} }\n"
    "Prefer 'assistant' steps for anything reasoning/writing. Keep it to 2–6 steps. "
    "Return JSON only — no markdown fences, no commentary."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "question": {"type": "string"},
                    "skill_names": {"type": "array", "items": {"type": "string"}},
                    "tool_name": {"type": "string"},
                    "args": {"type": "object"},
                    "variables": {"type": "object"},
                    "variable": {"type": "string"},
                    "channel": {"type": "string"},
                    "severity": {"type": "string"},
                    "subject": {"type": "string"},
                    "message": {"type": "string"},
                    "method": {"type": "string"},
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                },
                "required": ["name", "type"],
            },
        },
    },
    "required": ["name", "operations"],
}


def resolve_workflow_ai_model(db: Session) -> Optional[str]:
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    # Per-wizard slot, else the shared wizard slot. NO other fallback.
    for slot in ("wizard.workflow", "wizard.generator"):
        try:
            r = reg.resolve_slot(slot)
        except Exception:  # noqa: BLE001
            r = None
        if r and getattr(r, "model_id", None):
            return r.model_id
    return None


def _resolve_agent_id(db: Session) -> int:
    """The single agent's id for assistant operations (first planner, else first)."""
    from services.assistants.assistant_service import AssistantService
    rows = AssistantService(db).get_all(limit=100) or []
    planner = next((a for a in rows if (getattr(a, "assistant_type", "") or "").lower() == "planner"), None)
    a = planner or (rows[0] if rows else None)
    return int(getattr(a, "id", 0) or 0)


def _op_config(step: Dict[str, Any]) -> Dict[str, Any]:
    t = step.get("type")
    if t == "assistant":
        return {"question": step.get("question") or "Execute this step.", "skill_names": step.get("skill_names") or []}
    if t == "tool":
        return {"tool_name": step.get("tool_name") or "", "args": step.get("args") if isinstance(step.get("args"), dict) else {}}
    if t == "set_variable":
        return {"variables": step.get("variables") if isinstance(step.get("variables"), dict) else {}}
    if t == "new_thread":
        return {"variable": step.get("variable") or "thread"}
    if t == "notification":
        return {"channel": step.get("channel") or "ui", "severity": step.get("severity") or "info",
                "subject": step.get("subject") or "", "message": step.get("message") or ""}
    if t == "http_request":
        return {"method": step.get("method") or "GET", "url": step.get("url") or "",
                "headers": step.get("headers") if isinstance(step.get("headers"), dict) else {},
                "response_mode": "json", "fail_on_non_2xx": True}
    return {}


async def generate_and_create(db: Session, answers: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a workflow from the description, CREATE it disabled, and return
    the created workflow (so the FE can open it for review)."""
    model = resolve_workflow_ai_model(db)
    if not model:
        raise RuntimeError(
            "No model is assigned to generate workflows. Assign one to the "
            "AI wizards slot (or the Workflow wizard slot) under AI Models → Routing."
        )

    description = (answers or {}).get("description") or (answers or {}).get("goal") or ""
    if not str(description).strip():
        raise RuntimeError("Describe what the workflow should do first.")

    from services.openai_service import OpenAIResponsesService
    from services.providers.provider_factory import build_llm_router
    router = build_llm_router(OpenAIResponsesService(), db)
    resp = await router.ask_orchestration_async(
        f"Workflow to design:\n{description}",
        role="cognition:workflow_generator",
        model=model,
        instructions=_INSTRUCTIONS,
        keep_context=False,
        store=False,
        session_id=None,
        max_output_tokens=2500,
        json_schema=_SCHEMA,
    )
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise RuntimeError(f"The model ({model}) returned no output.")
    try:
        data = _extract_json(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not parse the model's workflow draft: {exc}")

    steps = data.get("operations")
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("The model did not return any workflow steps.")

    agent_id = _resolve_agent_id(db)
    from schemas.workflow import WorkflowCreate, WorkflowOperationCreate
    from services.workflows.workflow_service import WorkflowService

    ops: List[WorkflowOperationCreate] = []
    prev_pos: Optional[int] = None
    for i, step in enumerate(steps):
        t = (step.get("type") or "assistant").strip()
        if t not in _ALLOWED:
            t = "assistant"  # unknown → safe default
        position = (i + 1) * 100
        ops.append(WorkflowOperationCreate(
            name=(step.get("name") or f"Step {i + 1}").strip(),
            operation_type=t,
            operation_ref_id=agent_id if t == "assistant" else 0,
            config=_op_config({**step, "type": t}),
            depends_on=[prev_pos] if prev_pos is not None else [],
            position=position,
        ))
        prev_pos = position

    name = (data.get("name") or "AI workflow").strip()
    svc = WorkflowService(db)
    existing = {w.name for w in svc.get_all(limit=1000)}
    final = name if name not in existing else f"{name} (AI)"
    n = 2
    while final in existing:
        final = f"{name} (AI {n})"; n += 1

    created = svc.create(WorkflowCreate(
        name=final,
        description=data.get("description"),
        is_enabled=False,  # review before enabling
        operations=ops,
    ))
    log.infox("Workflow via AI aangemaakt", name=final, workflow_id=created.id, steps=len(ops), model=model)
    return {"id": created.id, "name": final, "steps": len(ops), "_model": model}


def _current_definition(wf: Any) -> str:
    """Compact text view of an existing workflow for the improve prompt."""
    lines = [f"name: {wf.name}", f"description: {wf.description or ''}", "operations:"]
    for op in sorted(getattr(wf, "operations", []) or [], key=lambda o: o.position):
        cfg = op.config or {}
        hint = cfg.get("question") or cfg.get("tool_name") or cfg.get("message") or cfg.get("variable") or ""
        lines.append(f"  - [{op.operation_type}] {op.name}: {str(hint)[:120]}")
    return "\n".join(lines)


def _build_ops(steps: List[Dict[str, Any]], agent_id: int):
    from schemas.workflow import WorkflowOperationCreate
    ops: List[Any] = []
    prev_pos: Optional[int] = None
    for i, step in enumerate(steps):
        t = (step.get("type") or "assistant").strip()
        if t not in _ALLOWED:
            t = "assistant"
        position = (i + 1) * 100
        ops.append(WorkflowOperationCreate(
            name=(step.get("name") or f"Step {i + 1}").strip(),
            operation_type=t,
            operation_ref_id=agent_id if t == "assistant" else 0,
            config=_op_config({**step, "type": t}),
            depends_on=[prev_pos] if prev_pos is not None else [],
            position=position,
        ))
        prev_pos = position
    return ops


async def improve_and_update(db: Session, workflow_id: int, instruction: str) -> Dict[str, Any]:
    """Improve an existing workflow with AI per the instruction, and update it in
    place. Returns {id, name, steps}. No model assigned → the wizard is off."""
    model = resolve_workflow_ai_model(db)
    if not model:
        raise RuntimeError(
            "No model is assigned to improve workflows. Assign one to the "
            "AI wizards slot (or the Workflow wizard slot) under AI Models → Routing."
        )
    from services.workflows.workflow_service import WorkflowService
    from schemas.workflow import WorkflowUpdate
    svc = WorkflowService(db)
    wf = svc.get_by_id(workflow_id)
    if wf is None:
        raise RuntimeError("Workflow not found.")

    instruction = (instruction or "").strip() or "Improve this workflow: fix issues, tighten the steps, and make it more effective."

    from services.openai_service import OpenAIResponsesService
    from services.providers.provider_factory import build_llm_router
    router = build_llm_router(OpenAIResponsesService(), db)
    resp = await router.ask_orchestration_async(
        f"Existing workflow:\n{_current_definition(wf)}\n\nImprovement request:\n{instruction}\n\n"
        "Return the FULL improved workflow (same JSON shape), keeping what works and applying the request.",
        role="cognition:workflow_improver",
        model=model,
        instructions=_INSTRUCTIONS,
        keep_context=False,
        store=False,
        session_id=None,
        max_output_tokens=2500,
        json_schema=_SCHEMA,
    )
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise RuntimeError(f"The model ({model}) returned no output.")
    try:
        data = _extract_json(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not parse the model's improved workflow: {exc}")
    steps = data.get("operations")
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("The model returned no workflow steps.")

    ops = _build_ops(steps, _resolve_agent_id(db))
    updated = svc.update(workflow_id, WorkflowUpdate(
        name=(data.get("name") or wf.name).strip() or wf.name,
        description=data.get("description") if data.get("description") is not None else wf.description,
        operations=ops,
    ))
    if updated is None:
        raise RuntimeError("Failed to update the workflow.")
    log.infox("Workflow via AI verbeterd", workflow_id=workflow_id, steps=len(ops), model=model)
    return {"id": workflow_id, "name": updated.name, "steps": len(ops), "_model": model}


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return obj
