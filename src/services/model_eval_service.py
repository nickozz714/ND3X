"""
services/model_eval_service.py

Full-vs-light evaluation harness for chat models (TODO 1.3 + 1.5).

Runs a FIXED task set through the real orchestrator per (model × prompt mode),
scores task completion / JSON validity / instruction deviations from the turn
result + trace, and stores every run machine-readable under BASE_DIR/eval/ so
new model versions can be compared against the baseline. Each run's report also
embeds a comparison with the most recent previous run per (model, mode).

Triggering:
- POST /admin/providers/model-eval  (background task; local models are slow)
- optional periodic run via MODEL_EVAL_INTERVAL_HOURS (0 = off, the default —
  a full eval keeps a local model busy for many minutes).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from component.config import settings
from component.logging import get_logger

log = get_logger(__name__)

# Fixed task set: deliberately bound to always-on builtin tools so the harness
# works on any workspace. Each task declares how its result is judged.
EVAL_TASKS: List[Dict[str, Any]] = [
    {
        "id": "greeting",
        "question": "Say hello in one short sentence.",
        "expect": {"modes": ["final", "answer"], "no_tools": True},
    },
    {
        "id": "shell_tool",
        "question": "Use your shell tool to run the command 'date' and tell me exactly what it printed.",
        # Shell exec is confirmation-gated: the correct outcome of turn 1 is a
        # confirm_action pause carrying the RIGHT tool.
        "expect": {"modes": ["confirm_action"], "tool": "system__shell_exec"},
    },
    {
        "id": "doc_list",
        "question": "List the documents in my workspace using your tools.",
        "expect": {"modes": ["final", "answer"], "tool": "text__list_files"},
    },
]

_DEVIATION_EVENTS = ("plan_validation_failed", "tool_block_recovering", "tool_id_resolved")


def eval_dir() -> Path:
    base = Path(settings.BASE_DIR or ".")
    d = base / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trace_stats(trace: List[dict]) -> Dict[str, Any]:
    stats = {
        "planner_calls": 0,
        "planner_durations_s": [],
        "unparseable": 0,
        "validation_failures": 0,
        "deviations": 0,
    }
    for ev in trace or []:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("event") or ev.get("type") or ""
        if etype == "planner_call_end":
            stats["planner_calls"] += 1
            dur = ev.get("duration_s")
            if isinstance(dur, (int, float)):
                stats["planner_durations_s"].append(float(dur))
        elif etype == "error" and ev.get("error") == "planner_unparseable":
            stats["unparseable"] += 1
        elif etype == "plan_validation_failed":
            stats["validation_failures"] += 1
        if etype in _DEVIATION_EVENTS:
            stats["deviations"] += 1
    return stats


def _score_task(task: Dict[str, Any], result: Dict[str, Any], elapsed_s: float) -> Dict[str, Any]:
    expect = task.get("expect") or {}
    mode = (result.get("mode") or "").strip()
    answer = (result.get("answer") or "").strip()
    # Tools used anywhere in the turn: the result's tool_calls only carries the
    # FINAL hop's calls (empty on a multi-hop final), so also scan the trace's
    # tool_call events — that's where earlier hops' executions live.
    tool_names: List[str] = []
    for tc in result.get("tool_calls") or []:
        if isinstance(tc, dict):
            name = (tc.get("tool") or tc.get("name") or "").strip()
            if name:
                tool_names.append(name)
    for ev in result.get("trace") or []:
        if isinstance(ev, dict) and (ev.get("event") or ev.get("type")) == "tool_call":
            name = (ev.get("tool") or "").strip()
            if name and name not in tool_names:
                tool_names.append(name)

    problems: List[str] = []
    if expect.get("modes") and mode not in expect["modes"]:
        problems.append(f"mode '{mode}' not in expected {expect['modes']}")
    if not answer:
        problems.append("empty answer")
    if expect.get("no_tools") and tool_names:
        problems.append(f"unexpected tool calls: {tool_names}")
    if expect.get("tool") and expect["tool"] not in tool_names:
        problems.append(f"expected tool '{expect['tool']}', got {tool_names or 'none'}")

    stats = _trace_stats(result.get("trace") or [])
    durations = stats.pop("planner_durations_s")
    return {
        "task_id": task["id"],
        "completed": not problems,
        "problems": problems,
        "mode": mode,
        "tool_calls": tool_names,
        "answer_preview": answer[:200],
        "elapsed_s": round(elapsed_s, 1),
        "json_valid": stats["unparseable"] == 0,
        **stats,
        "planner_avg_s": round(sum(durations) / len(durations), 1) if durations else None,
    }


async def _run_case(model: str, prompt_mode: str, task: Dict[str, Any]) -> Dict[str, Any]:
    from services.assistants.ask_job_callbacks import run_ask_orchestrator
    thread_id = f"eval-{uuid.uuid4()}"
    payload = {
        "thread_id": thread_id,
        "forced_model": model,
        "model": model,
        "_light_mode_session": prompt_mode == "light",
    }
    t0 = time.time()
    try:
        result = await run_ask_orchestrator(
            question=task["question"], payload=payload, thread_id=thread_id, model=model,
        )
    except Exception as exc:  # noqa: BLE001 — a crashed case is a scored failure
        return {
            "task_id": task["id"], "completed": False, "json_valid": False,
            "problems": [f"run failed: {type(exc).__name__}: {str(exc)[:200]}"],
            "elapsed_s": round(time.time() - t0, 1),
            "mode": "exception", "tool_calls": [], "answer_preview": "",
            "planner_calls": 0, "unparseable": 0, "validation_failures": 0,
            "deviations": 0, "planner_avg_s": None,
        }
    return _score_task(task, result or {}, time.time() - t0)


def _summarize_pair(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(cases)
    completed = sum(1 for c in cases if c["completed"])
    return {
        "tasks": total,
        "completed": completed,
        "completion_rate": round(completed / total, 3) if total else 0.0,
        "json_valid_rate": round(sum(1 for c in cases if c["json_valid"]) / total, 3) if total else 0.0,
        "validation_failures": sum(c["validation_failures"] for c in cases),
        "deviations": sum(c["deviations"] for c in cases),
        "avg_elapsed_s": round(sum(c["elapsed_s"] for c in cases) / total, 1) if total else None,
    }


def list_runs() -> List[Dict[str, Any]]:
    """Newest-first index of stored eval runs (id + status + summary)."""
    runs = []
    for f in sorted(eval_dir().glob("eval_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            runs.append({
                "run_id": data.get("run_id"),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "status": data.get("status"),
                "pairs": {k: v.get("summary") for k, v in (data.get("results") or {}).items()},
            })
        except Exception:  # noqa: BLE001 — skip an unreadable file
            continue
    return runs


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    f = eval_dir() / f"eval_{run_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return None


def _previous_summary(run_id: str, pair_key: str) -> Optional[Dict[str, Any]]:
    for run in list_runs():
        if run.get("run_id") == run_id:
            continue
        summary = (run.get("pairs") or {}).get(pair_key)
        if summary:
            return {"run_id": run.get("run_id"), "finished_at": run.get("finished_at"), "summary": summary}
    return None


async def run_eval(
    *,
    models: List[str],
    modes: Optional[List[str]] = None,
    task_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the fixed task set for every model × prompt mode, score it, store the
    machine-readable report (BASE_DIR/eval/eval_<run_id>.json) and return it.
    Cases run sequentially — local models share one GPU."""
    modes = [m for m in (modes or ["full", "light"]) if m in ("full", "light")] or ["full", "light"]
    tasks = [t for t in EVAL_TASKS if not task_ids or t["id"] in task_ids]
    run_id = time.strftime("%Y%m%d-%H%M%S")
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "running",
        "models": models,
        "modes": modes,
        "task_ids": [t["id"] for t in tasks],
        "results": {},
    }
    out_file = eval_dir() / f"eval_{run_id}.json"
    out_file.write_text(json.dumps(report, indent=2))

    for model in models:
        for mode in modes:
            pair_key = f"{model}|{mode}"
            log.infox("Model-eval pair gestart", model=model, prompt_mode=mode, run_id=run_id)
            cases = []
            for task in tasks:
                cases.append(await _run_case(model, mode, task))
            report["results"][pair_key] = {
                "model": model,
                "prompt_mode": mode,
                "summary": _summarize_pair(cases),
                "cases": cases,
            }
            out_file.write_text(json.dumps(report, indent=2))  # progress survives a crash

    # Baseline comparison (TODO 1.5): diff each pair against its most recent
    # previous run so a new model version's regression is immediately visible.
    for pair_key, pair in report["results"].items():
        prev = _previous_summary(run_id, pair_key)
        if prev:
            cur, old = pair["summary"], prev["summary"]
            pair["baseline"] = {
                "compared_to_run": prev["run_id"],
                "completion_rate_delta": round(cur["completion_rate"] - old["completion_rate"], 3),
                "avg_elapsed_delta_s": (
                    round(cur["avg_elapsed_s"] - old["avg_elapsed_s"], 1)
                    if cur.get("avg_elapsed_s") is not None and old.get("avg_elapsed_s") is not None
                    else None
                ),
            }

    report["status"] = "completed"
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out_file.write_text(json.dumps(report, indent=2))
    log.infox("Model-eval run afgerond", run_id=run_id, pairs=len(report["results"]))
    return report


async def run_scheduled_eval() -> None:
    """Periodic evaluation (MODEL_EVAL_INTERVAL_HOURS > 0): evaluates the LOCAL
    chat models currently assigned to chat routing slots, both prompt modes."""
    try:
        from db.database import SessionLocal
        from services.providers.registry_service import ProviderRegistryService
        with SessionLocal() as db:
            reg = ProviderRegistryService(db)
            models: List[str] = []
            for slot in ("chat.planner", "chat.cognition"):
                r = reg.resolve_slot(slot)
                mid = getattr(r, "model_id", None) if r else None
                if mid and mid not in models and reg.model_is_local(mid):
                    models.append(mid)
        if not models:
            log.infox("Scheduled model-eval overgeslagen: geen lokale chat-slotmodellen")
            return
        await run_eval(models=models)
    except Exception as exc:  # noqa: BLE001 — a failed eval must not kill the scheduler
        log.warningx("Scheduled model-eval mislukt", error=str(exc))
