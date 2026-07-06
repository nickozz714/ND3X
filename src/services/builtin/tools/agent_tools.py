"""
services/builtin/tools/agent_tools.py

Internal tool om subagents te dispatchen (Claude-Code-stijl "Task"/"Agent").
Een assistant kan binnen zijn agent-loop werk delegeren aan een verse subagent
die in een eigen thread (schone context) draait en een gecondenseerd resultaat
teruggeeft.

Twee modes:
  * named  — `assistant` opgegeven => die assistant wordt geforceerd uitgevoerd
             (via de bestaande `force_assistant` router-knop).
  * ad-hoc — geen `assistant` => de normale router kiest, of een geconfigureerde
             SUBAGENT_DEFAULT_ASSISTANT wordt geforceerd. Capabilities kunnen
             worden ingeperkt met `skills`.

Meerdere `agent__dispatch` calls binnen één turn draaien automatisch parallel
dankzij de dependency-aware tool-scheduler (Phase 1), zonder placeholder-
afhankelijkheid.

Wordt geregistreerd bij import — zorg dat dit bestand geïmporteerd wordt in
ask_job_callbacks.py zodat de tool beschikbaar is.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional

from component.config import settings
from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)

# Diepte van geneste subagent-dispatch in de huidige uitvoeringscontext.
# Top-level (user) run = 0; elke geneste dispatch verhoogt met 1. ContextVars
# worden per asyncio-task gekopieerd, dus parallelle dispatches tellen
# onafhankelijk en correct.
_subagent_depth: ContextVar[int] = ContextVar("subagent_depth", default=0)


def current_subagent_depth() -> int:
    return int(_subagent_depth.get())


@contextmanager
def _enter_subagent(depth: int):
    token = _subagent_depth.set(depth)
    try:
        yield
    finally:
        _subagent_depth.reset(token)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _build_task_prompt(task: str, context: Optional[str]) -> str:
    task = (task or "").strip()
    context = (context or "").strip()
    if not context:
        return task
    return f"{task}\n\n## Context from the dispatching agent\n{context}"


def _condense_result(
    *,
    out: Dict[str, Any],
    forced_assistant: Optional[str],
    thread_id: str,
) -> Dict[str, Any]:
    """Vouw het volledige subagent-resultaat samen tot een compacte handoff."""
    out = out if isinstance(out, dict) else {}
    handoff = out.get("downstream_handoff") if isinstance(out.get("downstream_handoff"), dict) else {}
    mode = str(out.get("mode") or "").lower()
    terminal_state = out.get("terminal_state")
    answer = str(out.get("answer") or "")

    failed = mode in {"error", "failed"} or str(terminal_state or "").lower() in {
        "failed",
        "budget_exceeded",
        "cancelled",
        "policy_denied",
    }

    summary = handoff.get("summary") or _truncate(answer, int(getattr(settings, "SUBAGENT_SUMMARY_MAX_CHARS", 4000)))

    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = out.get("artifacts") if isinstance(out.get("artifacts"), list) else []

    return {
        "status": "error" if failed else "ok",
        "assistant": forced_assistant or "auto",
        "thread_id": thread_id,
        "summary": summary,
        "facts": handoff.get("facts") if isinstance(handoff.get("facts"), dict) else {},
        "artifacts": artifacts,
        "open_questions": handoff.get("open_questions") if isinstance(handoff.get("open_questions"), list) else [],
        "terminal_state": terminal_state,
        "tool_call_count": len(out.get("tool_calls") or []),
    }


@internal_tool_registry.register(
    name="agent__dispatch",
    title="Dispatch Subagent",
    description=(
        "Delegate a self-contained task to a fresh subagent that runs in its own "
        "thread with a clean context and returns a condensed result (summary, "
        "facts, artifacts, open_questions). Use for parallelizable or well-scoped "
        "subtasks (research, drafting, analysis). Provide `assistant` to dispatch a "
        "specific assistant by name, or omit it for an ad-hoc general-purpose agent. "
        "Issue multiple dispatch calls in one turn to fan out work in parallel."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The complete, self-contained instruction for the subagent.",
            },
            "assistant": {
                "type": "string",
                "description": "Optional name of an existing assistant to run. Omit for ad-hoc.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional skill names to scope the subagent's capabilities.",
            },
            "context": {
                "type": "string",
                "description": "Optional extra context/background to hand to the subagent.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override; defaults to the configured LLM model.",
            },
        },
        "required": ["task"],
    },
    tags=["internal", "agent", "orchestration"],
)
async def agent_dispatch(args: Dict[str, Any]) -> Dict[str, Any]:
    task = str((args or {}).get("task") or "").strip()
    if not task:
        return {"status": "error", "error": "agent__dispatch requires a non-empty 'task'."}

    assistant = (args.get("assistant") or "").strip() or None
    skills = args.get("skills") if isinstance(args.get("skills"), list) else None
    context = args.get("context")
    model = (args.get("model") or "").strip() or None  # None → resolved from slot

    depth = current_subagent_depth()
    max_depth = int(getattr(settings, "SUBAGENT_MAX_DEPTH", 3))
    if depth >= max_depth:
        log.warningx("Subagent dispatch geweigerd: max diepte bereikt", depth=depth, max_depth=max_depth)
        return {
            "status": "error",
            "error": f"Subagent dispatch refused: max nesting depth {max_depth} reached.",
        }

    forced_assistant = assistant or (getattr(settings, "SUBAGENT_DEFAULT_ASSISTANT", "") or None)
    thread_id = f"subagent-{uuid.uuid4().hex[:12]}"

    payload: Dict[str, Any] = {
        "_subagent": True,
        "_subagent_depth": depth + 1,
    }
    if forced_assistant:
        payload["force_assistant"] = forced_assistant
    if skills:
        payload["_selected_skill_names"] = [str(s).strip() for s in skills if str(s).strip()]

    log.infox(
        "Subagent dispatch gestart",
        mode="named" if assistant else "ad-hoc",
        forced_assistant=forced_assistant,
        depth=depth + 1,
        thread_id=thread_id,
        skill_count=len(skills or []),
        task_preview=_truncate(task, 160),
    )

    # Lazy import om circulaire import met ask_job_callbacks te vermijden.
    from services.assistants.ask_job_callbacks import run_ask_orchestrator

    try:
        with _enter_subagent(depth + 1):
            out = await run_ask_orchestrator(
                question=_build_task_prompt(task, context),
                payload=payload,
                thread_id=thread_id,
                model=model,
            )
    except Exception as exc:  # noqa: BLE001 — subagent-fout mag de parent niet laten crashen
        log.exceptionx("Subagent dispatch mislukt", thread_id=thread_id, exception=exc)
        return {
            "status": "error",
            "assistant": forced_assistant or "auto",
            "thread_id": thread_id,
            "error": f"{type(exc).__name__}: {exc}",
        }

    result = _condense_result(out=out, forced_assistant=forced_assistant, thread_id=thread_id)
    log.infox(
        "Subagent dispatch afgerond",
        thread_id=thread_id,
        status=result.get("status"),
        terminal_state=result.get("terminal_state"),
        summary_length=len(result.get("summary") or ""),
        artifact_count=len(result.get("artifacts") or []),
    )
    return result
