"""
services/builtin/tools/background_tasks.py

Internal tools om werk op de achtergrond te draaien (Claude-Code-stijl
"run_in_background"). De assistant kan een taak starten, dóórwerken, en later
de status/resultaten ophalen. Voltooide achtergrondtaken worden bovendien per
agent-loop-iteratie "gedraind" en als trace-notificatie aan de parent getoond.

Een achtergrondtaak hergebruikt de subagent-dispatch (agent__dispatch): de taak
draait als losgekoppelde asyncio-task in een eigen thread/context.

Wordt geregistreerd bij import — zorg dat dit bestand geïmporteerd wordt in
ask_job_callbacks.py.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from component.config import settings
from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)

# Thread-id van de lopende (parent) run; gezet door de pipeline runner vóór
# tool-executie, zodat achtergrondtaken aan hun eigenaar gekoppeld worden.
current_run_thread: ContextVar[Optional[str]] = ContextVar("current_run_thread", default=None)

# In-memory registry van achtergrondtaken.
_TASKS: Dict[str, Dict[str, Any]] = {}
_TASKS_LOCK = asyncio.Lock()
_TASK_HANDLES: Dict[str, "asyncio.Task[Any]"] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": rec["id"],
        "status": rec["status"],
        "task_preview": rec.get("task_preview"),
        "assistant": rec.get("assistant"),
        "created_at": rec.get("created_at"),
        "finished_at": rec.get("finished_at"),
    }


async def _run_background_task(task_id: str, args: Dict[str, Any]) -> None:
    """Draai de subagent op de achtergrond en bewaar het resultaat."""
    from services.builtin.tools.agent_tools import agent_dispatch

    try:
        result = await agent_dispatch(args)
        status = "error" if (isinstance(result, dict) and result.get("status") == "error") else "done"
        async with _TASKS_LOCK:
            rec = _TASKS.get(task_id)
            if rec is not None:
                rec["status"] = status
                rec["result"] = result
                rec["finished_at"] = _now_ms()
        log.infox("Achtergrondtaak afgerond", task_id=task_id, status=status)
    except asyncio.CancelledError:
        async with _TASKS_LOCK:
            rec = _TASKS.get(task_id)
            if rec is not None:
                rec["status"] = "cancelled"
                rec["finished_at"] = _now_ms()
        raise
    except Exception as exc:  # noqa: BLE001 — fout mag de event loop niet breken
        log.exceptionx("Achtergrondtaak mislukt", task_id=task_id, exception=exc)
        async with _TASKS_LOCK:
            rec = _TASKS.get(task_id)
            if rec is not None:
                rec["status"] = "error"
                rec["result"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                rec["finished_at"] = _now_ms()
    finally:
        _TASK_HANDLES.pop(task_id, None)


async def drain_completed_background_tasks(thread_id: Optional[str]) -> List[Dict[str, Any]]:
    """Geef voltooide-maar-nog-niet-bevestigde taken voor ``thread_id`` terug en
    markeer ze als bevestigd. Gebruikt door de agent-loop om notificaties te tonen.
    """
    drained: List[Dict[str, Any]] = []
    async with _TASKS_LOCK:
        for rec in _TASKS.values():
            if rec.get("owner_thread") != thread_id:
                continue
            if rec["status"] in {"done", "error", "cancelled"} and not rec.get("_acknowledged"):
                rec["_acknowledged"] = True
                drained.append({
                    "task_id": rec["id"],
                    "status": rec["status"],
                    "assistant": rec.get("assistant"),
                    "summary": (rec.get("result") or {}).get("summary") if isinstance(rec.get("result"), dict) else None,
                    "task_preview": rec.get("task_preview"),
                })
    return drained


# ── task__create ───────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="task__create",
    title="Start Background Task",
    description=(
        "Launch a task in the background and immediately return a task_id so you "
        "can keep working. The task runs as a detached subagent (same arguments as "
        "agent__dispatch). Poll task__status / task__result to retrieve the outcome; "
        "completed tasks are also surfaced automatically on later loop iterations. "
        "NOTE: on a LOCAL model the task queues behind your own steps (one model, "
        "one queue) — it finishes shortly after your turn, not in parallel; pass a "
        "different 'model' if true parallelism matters."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The complete, self-contained instruction for the background subagent."},
            "assistant": {"type": "string", "description": "Optional name of an existing assistant to run. Omit for ad-hoc."},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Optional skill names to scope capabilities."},
            "context": {"type": "string", "description": "Optional extra context/background."},
            "model": {"type": "string", "description": "Optional model override."},
        },
        "required": ["task"],
    },
    tags=["internal", "agent", "background"],
)
async def task_create(args: Dict[str, Any]) -> Dict[str, Any]:
    task = str((args or {}).get("task") or "").strip()
    if not task:
        return {"status": "error", "error": "task__create requires a non-empty 'task'."}

    max_active = int(getattr(settings, "BACKGROUND_TASK_MAX_ACTIVE", 16))
    async with _TASKS_LOCK:
        active = sum(1 for r in _TASKS.values() if r["status"] == "running")
        if active >= max_active:
            return {"status": "error", "error": f"Too many active background tasks ({active}/{max_active})."}

    task_id = f"bg-{uuid.uuid4().hex[:12]}"
    owner_thread = current_run_thread.get()
    rec = {
        "id": task_id,
        "status": "running",
        "owner_thread": owner_thread,
        "assistant": (args.get("assistant") or "ad-hoc"),
        "task_preview": task[:160],
        "created_at": _now_ms(),
        "finished_at": None,
        "result": None,
        "_acknowledged": False,
    }
    async with _TASKS_LOCK:
        _TASKS[task_id] = rec

    # Losgekoppelde task; de huidige context wordt gekopieerd bij creatie.
    handle = asyncio.create_task(_run_background_task(task_id, dict(args)), name=f"bgtask-{task_id}")
    _TASK_HANDLES[task_id] = handle

    log.infox("Achtergrondtaak gestart", task_id=task_id, owner_thread=owner_thread, task_preview=task[:120])
    return {"status": "started", "task_id": task_id, "message": "Task running in background; poll task__status / task__result."}


# ── task__status ───────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="task__status",
    title="Background Task Status",
    description="Return the status of a background task (running/done/error/cancelled) by task_id.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    tags=["internal", "agent", "background"],
)
async def task_status(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str((args or {}).get("task_id") or "").strip()
    async with _TASKS_LOCK:
        rec = _TASKS.get(task_id)
        if rec is None:
            return {"status": "error", "error": f"Unknown task_id {task_id!r}."}
        return _public_view(rec)


# ── task__result ───────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="task__result",
    title="Background Task Result",
    description="Return the condensed result of a completed background task, or its status if still running.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    tags=["internal", "agent", "background"],
)
async def task_result(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str((args or {}).get("task_id") or "").strip()
    async with _TASKS_LOCK:
        rec = _TASKS.get(task_id)
        if rec is None:
            return {"status": "error", "error": f"Unknown task_id {task_id!r}."}
        if rec["status"] == "running":
            return {"task_id": task_id, "status": "running", "message": "Still running; try again later."}
        return {"task_id": task_id, "status": rec["status"], "result": rec.get("result")}


# ── task__list ─────────────────────────────────────────────────────────────────

@internal_tool_registry.register(
    name="task__list",
    title="List Background Tasks",
    description="List background tasks started by the current run (most recent first).",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "agent", "background"],
)
async def task_list(_args: Dict[str, Any]) -> Dict[str, Any]:
    owner_thread = current_run_thread.get()
    async with _TASKS_LOCK:
        items = [
            _public_view(rec)
            for rec in _TASKS.values()
            if owner_thread is None or rec.get("owner_thread") == owner_thread
        ]
    items.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {"status": "ok", "tasks": items, "count": len(items)}
