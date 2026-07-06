import asyncio
import json
import shutil
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from services.voice.voice_utilities import safe_slug


ProgressCallback = Callable[[Dict[str, Any]], None]
RunAskCallback = Callable[..., Awaitable[Dict[str, Any]]]
StoreUserMessageCallback = Callable[..., Awaitable[None]]
StoreAssistantMessageCallback = Callable[..., Awaitable[None]]
ResponseCallback = Callable[..., Dict[str, Any]]


TERMINAL_ASK_STATES: Set[str] = {"completed", "failed", "timed_out", "rejected", "cancelled"}
ACTIVE_ASK_STATES: Set[str] = {"queued", "running", "awaiting_confirmation"}


# ── Active-run registry (for the local-model queue indicator) ────────────────
# In-process record of runs currently executing, keyed by run_id. Used to tell
# the FE when a run is effectively queued behind another run on the SAME local
# model — Ollama serializes those (OLLAMA_NUM_PARALLEL=1), so the second chat
# waits. Execution is NOT gated here (an app-level lock would deadlock with
# subagents); this is purely an observability signal.
_ACTIVE_RUNS: Dict[str, Dict[str, Any]] = {}


def _register_active_run(run_id: str, model: str, is_local: bool, parallel: int = 1) -> None:
    import time as _t
    _ACTIVE_RUNS[run_id] = {
        "model": (model or "").strip(), "is_local": bool(is_local),
        "parallel": max(1, int(parallel or 1)), "started": _t.time(),
    }


def _unregister_active_run(run_id: str) -> None:
    _ACTIVE_RUNS.pop(run_id, None)


def queue_info(run_id: str) -> Dict[str, Any]:
    """How many active runs on the same LOCAL model started before this one.
    ahead>0 → this run is waiting for the local model (another chat holds it)."""
    me = _ACTIVE_RUNS.get(run_id)
    if not me or not me.get("is_local") or not me.get("model"):
        return {"queued": False, "ahead": 0}
    ahead = sum(
        1 for rid, r in _ACTIVE_RUNS.items()
        if rid != run_id and r.get("is_local") and r.get("model") == me["model"]
        and r.get("started", 0) < me.get("started", 0)
    )
    # This model may run `parallel` turns at once (matches OLLAMA_NUM_PARALLEL);
    # only the ones beyond that are actually waiting.
    parallel = max(1, int(me.get("parallel") or 1))
    waiting = max(0, ahead - (parallel - 1))
    return {"queued": waiting > 0, "ahead": waiting, "model": me["model"]}



# Voice job schemas are not guaranteed to be identical to ask jobs, so cleanup
# accepts a wider set of status names for generic disk-backed runtime jobs.
TERMINAL_RUNTIME_STATES: Set[str] = TERMINAL_ASK_STATES | {
    "done",
    "success",
    "succeeded",
    "finished",
    "complete",
    "cancelled",
    "canceled",
    "error",
}
ACTIVE_RUNTIME_STATES: Set[str] = ACTIVE_ASK_STATES | {
    "processing",
    "pending",
    "started",
    "starting",
    "transcribing",
    "generating",
    "stopping",
}


class AskJobService:
    """
    Disk-backed async ask job service plus runtime job cleanup.

    Stores ask runs under:
        ask_root/<safe_thread_id>/<run_id>/request.json
        ask_root/<safe_thread_id>/<run_id>/status.json
        ask_root/<safe_thread_id>/<run_id>/result.json

    Also cleans voice jobs under:
        voice_root/<safe_thread_id>/<run_id>/...

    The service owns disk persistence, polling state, background ask execution,
    and cleanup. The route/application layer keeps ownership of orchestration,
    DB/session lifecycle, and transcript persistence by passing callbacks per job.
    """

    def __init__(
        self,
        *,
        ask_root: Path,
        voice_root: Optional[Path] = None,
        cleanup_interval_seconds: int = 60 * 60,
        run_retention_hours: int = 24,
        active_retention_hours: int = 6,
        voice_retention_hours: int = 24,
        voice_active_retention_hours: int = 6,
    ) -> None:
        self.ask_root = ask_root
        self.voice_root = voice_root
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.run_retention_hours = run_retention_hours
        self.active_retention_hours = active_retention_hours
        self.voice_retention_hours = voice_retention_hours
        self.voice_active_retention_hours = voice_active_retention_hours
        # In-flight ask tasks, keyed by "<thread_id>/<run_id>", so a cancel
        # request can interrupt the running orchestration (and its in-flight
        # provider call) via asyncio task cancellation.
        self._running_tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def _task_key(thread_id: str, run_id: str) -> str:
        return f"{thread_id}/{run_id}"

    # ---------------------------------------------------------------------
    # Basic file helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def ensure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
        AskJobService.ensure_dir(path.parent)

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)

        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)

    @staticmethod
    def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not path.exists():
            return default or {}

        try:
            raw = path.read_text(encoding="utf-8")
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else (default or {})
        except Exception:
            return default or {}

    @staticmethod
    def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def path_mtime_utc(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    # ---------------------------------------------------------------------
    # Path helpers
    # ---------------------------------------------------------------------

    def thread_dir(self, thread_id: str) -> Path:
        return self.ask_root / safe_slug(thread_id)

    def run_dir(self, thread_id: str, run_id: str) -> Path:
        return self.thread_dir(thread_id) / run_id

    def status_path(self, thread_id: str, run_id: str) -> Path:
        return self.run_dir(thread_id, run_id) / "status.json"

    def result_path(self, thread_id: str, run_id: str) -> Path:
        return self.run_dir(thread_id, run_id) / "result.json"

    def request_path(self, thread_id: str, run_id: str) -> Path:
        return self.run_dir(thread_id, run_id) / "request.json"

    # ---------------------------------------------------------------------
    # Status/result persistence
    # ---------------------------------------------------------------------

    def write_status(
        self,
        *,
        thread_id: str,
        run_id: str,
        state: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "state": state,
            "updated_at": self.utc_now(),
        }

        if extra:
            payload.update(extra)

        self.write_json_atomic(self.status_path(thread_id, run_id), payload)
        return payload

    def write_result(
        self,
        *,
        thread_id: str,
        run_id: str,
        state: str,
        result: Dict[str, Any],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "state": state,
            "result": result,
            "completed_at": self.utc_now(),
        }

        if extra:
            payload.update(extra)

        self.write_json_atomic(self.result_path(thread_id, run_id), payload)
        return payload

    def get_status(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        status = self.read_json(
            self.status_path(thread_id, run_id),
            default={
                "thread_id": thread_id,
                "run_id": run_id,
                "state": "not_found",
                "updated_at": self.utc_now(),
            },
        )
        # Live local-model queue signal (not persisted in status.json): computed
        # each read so it clears as soon as the run ahead finishes.
        if status.get("state") in ACTIVE_ASK_STATES:
            q = queue_info(run_id)
            if q.get("queued"):
                status["queue"] = q
        return status

    def get_result(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        return self.read_json(
            self.result_path(thread_id, run_id),
            default={
                "thread_id": thread_id,
                "run_id": run_id,
                "state": "not_ready",
            },
        )

    # ---------------------------------------------------------------------
    # Response helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def build_polling_envelope(thread_id: str, run_id: str, state: str = "queued") -> Dict[str, Any]:
        return {
            "mode": "processing",
            "thread_id": thread_id,
            "run_id": run_id,
            "answer": "Processing your request. Poll for status and result.",
            "pending_action": {
                "type": "poll",
                "state": state,
                "status_url": f"/main/ask/{thread_id}/{run_id}",
                "result_url": f"/main/ask/{thread_id}/{run_id}/result",
            },
            "tool_calls": [],
            "tool_results": [],
            "docs": [],
            "trace": [],
        }

    def map_trace_event_to_status(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_type = event.get("type")
        summary = event.get("summary") or ""

        base = {
            "event_type": event_type,
            "summary": summary,
            "trace_seq": event.get("seq"),
            "turn_id": event.get("turn_id"),
            "level": event.get("level"),
            "updated_at": self.utc_now(),
        }

        if event_type == "turn_start":
            return {"state": "running", "phase": "starting", "message": "Starting request", **base}

        if event_type == "router_plan":
            route = event.get("route") or {}
            return {
                "state": "running",
                "phase": "routing",
                "message": summary or "Choosing assistant workflow",
                "route_mode": route.get("mode"),
                **base,
            }

        if event_type == "assistant_step_start":
            step = event.get("step") or {}
            return {
                "state": "running",
                "phase": "assistant_step",
                "message": summary or "Running assistant step",
                "step": step.get("step"),
                "assistant": step.get("assistant") or step.get("assistant_name"),
                **base,
            }

        if event_type == "plan":
            return {
                "state": "running",
                "phase": "planning",
                "message": summary or "Planning",
                "assistant": event.get("assistant"),
                **base,
            }

        if event_type == "agent_narration":
            # The model's user-facing running commentary for this step (chat only).
            # The FE renders these as persistent narration bubbles.
            return {
                "state": "running",
                "phase": "narration",
                "kind": "narration",
                "message": event.get("say") or summary,
                "assistant": event.get("assistant"),
                **base,
            }

        if event_type == "tool_call":
            return {
                "state": "running",
                "phase": "tool_call",
                "message": summary or "Calling tool",
                "assistant": event.get("assistant"),
                "tool": event.get("tool"),
                "tool_id": event.get("tool_id"),
                **base,
            }

        if event_type == "tool_result":
            return {
                "state": "running",
                "phase": "tool_result",
                "message": summary or "Tool finished",
                "assistant": event.get("assistant"),
                "tool": event.get("tool"),
                "duration_ms": event.get("duration_ms"),
                **base,
            }

        if event_type == "confirm_prompt":
            return {
                "state": "awaiting_confirmation",
                "phase": "confirmation",
                "message": summary or "Waiting for confirmation",
                **base,
            }

        if event_type == "assistant_turn_end":
            return {
                "state": "running",
                "phase": "assistant_done",
                "message": summary or "Assistant step completed",
                "assistant": event.get("assistant"),
                **base,
            }

        if event_type == "answer_partial":
            # Streamed final answer building up live. Surfaced via the existing poll; the FE
            # renders partial_answer growing and tightens its poll interval while answering.
            return {
                "state": "running",
                "phase": "answering",
                "message": "Answering",
                "partial_answer": event.get("partial_answer") or "",
                **base,
            }

        if event_type == "turn_end":
            return {
                "state": "running",
                "phase": "finalizing",
                "message": summary or "Finalizing response",
                **base,
            }

        if event_type == "error":
            return {
                "state": "running",
                "phase": "error",
                "message": summary or "Error encountered",
                **base,
            }

        return {
            "state": "running",
            "phase": "working",
            "message": summary or "Working",
            **base,
        }

    # ---------------------------------------------------------------------
    # Job lifecycle
    # ---------------------------------------------------------------------

    async def create_job(
        self,
        *,
        question: str,
        payload: Dict[str, Any],
        thread_id: str,
        model: str,
        run_ask_cb: RunAskCallback,
        store_user_message_cb: StoreUserMessageCallback,
        store_assistant_message_cb: StoreAssistantMessageCallback,
        timeout_response_cb: ResponseCallback,
        error_response_cb: ResponseCallback,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        run_dir = self.run_dir(thread_id, run_id)
        self.ensure_dir(run_dir)

        await store_user_message_cb(
            thread_id=thread_id,
            question=question,
            payload=payload,
            turn_id=None,
        )

        self.write_json_atomic(
            self.request_path(thread_id, run_id),
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "question": question,
                "payload": payload,
                "model": model,
                "created_at": self.utc_now(),
            },
        )

        self.write_status(thread_id=thread_id, run_id=run_id, state="queued")

        task = asyncio.create_task(
            self.process_job(
                thread_id=thread_id,
                run_id=run_id,
                question=question,
                payload=payload,
                model=model,
                run_ask_cb=run_ask_cb,
                store_assistant_message_cb=store_assistant_message_cb,
                timeout_response_cb=timeout_response_cb,
                error_response_cb=error_response_cb,
            )
        )
        task.add_done_callback(self._log_background_task_exception)
        self._running_tasks[self._task_key(thread_id, run_id)] = task

        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state": "queued",
        }

    def cancel_job(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        """Request cancellation of an in-flight ask run.

        Cancels the asyncio task (which interrupts the awaited orchestration and
        its in-flight provider call) and marks the run cancelled so pollers stop.
        """
        current = self.get_status(thread_id=thread_id, run_id=run_id)
        state = current.get("state")
        if state in TERMINAL_ASK_STATES:
            return {"thread_id": thread_id, "run_id": run_id, "state": state, "cancelled": False, "reason": "already_terminal"}
        if state == "not_found":
            return {"thread_id": thread_id, "run_id": run_id, "state": state, "cancelled": False, "reason": "not_found"}

        task = self._running_tasks.get(self._task_key(thread_id, run_id))
        if task is not None and not task.done():
            task.cancel()  # process_job's CancelledError handler writes the terminal status

        # Mark intent immediately so polling reflects it even before the task unwinds.
        self.write_status(
            thread_id=thread_id,
            run_id=run_id,
            state="cancelled",
            extra={"phase": "cancelled", "message": "Request cancelled by user"},
        )
        return {"thread_id": thread_id, "run_id": run_id, "state": "cancelled", "cancelled": True}

    async def process_job(
        self,
        *,
        thread_id: str,
        run_id: str,
        question: str,
        payload: Dict[str, Any],
        model: str,
        run_ask_cb: RunAskCallback,
        store_assistant_message_cb: StoreAssistantMessageCallback,
        timeout_response_cb: ResponseCallback,
        error_response_cb: ResponseCallback,
    ) -> None:
        # Register in the active-run registry for the local-model queue indicator.
        # Effective model = forced pick, else the passed model, else the planner
        # slot (Auto). Best-effort; never blocks the run.
        try:
            eff_model = (payload.get("forced_model") or model or "").strip()
            from db.database import SessionLocal
            from services.providers.registry_service import ProviderRegistryService
            with SessionLocal() as _db:
                _reg = ProviderRegistryService(_db)
                if not eff_model:
                    r = _reg.resolve_slot("chat.planner")
                    eff_model = getattr(r, "model_id", "") if r else ""
                _is_local = _reg.model_is_local(eff_model)
                _parallel = _reg.model_num_parallel(eff_model) if _is_local else 1
            _register_active_run(run_id, eff_model, _is_local, _parallel)
        except Exception:  # noqa: BLE001
            pass

        self.write_status(
            thread_id=thread_id,
            run_id=run_id,
            state="running",
            extra={"phase": "starting", "message": "Starting request"},
        )

        # Running commentary accumulated across the turn. write_status REPLACES the
        # snapshot (no merge), and the FE polls ~1s, so a single transient "narration"
        # phase was almost never caught live — the steps only appeared at the end (from
        # the trace). Instead we append each narration/tool step here and stamp the FULL
        # list onto every status snapshot, so the live thread matches the final one.
        from services.assistants.ask_job_callbacks import steps_from_trace as _steps_from_trace
        live_steps: list = []

        def progress_cb(event: Dict[str, Any]) -> None:
            mapped = self.map_trace_event_to_status(event)
            state = mapped.pop("state", "running")

            # Do not let late progress events overwrite terminal states.
            current = self.get_status(thread_id=thread_id, run_id=run_id)
            if current.get("state") in TERMINAL_ASK_STATES:
                return

            for _st in _steps_from_trace([event]):
                if not live_steps or live_steps[-1] != _st:
                    live_steps.append(_st)
            mapped["steps"] = list(live_steps)

            self.write_status(
                thread_id=thread_id,
                run_id=run_id,
                state=state,
                extra=mapped,
            )

        # Cross-worker cancel: with RUNTIME_WORKERS > 1 the cancel request may hit a
        # different process than the one running this task, so the in-process
        # task.cancel() can't reach it. A watcher polls the shared status.json and
        # cancels the run when another worker marks it cancelled/stopping.
        async def _watch_for_cancel(run_task: asyncio.Task) -> None:
            while not run_task.done():
                await asyncio.sleep(1.0)
                state = self.get_status(thread_id=thread_id, run_id=run_id).get("state")
                if state in {"cancelled", "canceled", "stopping"}:
                    run_task.cancel()
                    return

        try:
            run_task = asyncio.ensure_future(
                run_ask_cb(
                    question=question,
                    payload=payload,
                    thread_id=thread_id,
                    model=model,
                    progress_cb=progress_cb,
                )
            )
            watch_task = asyncio.ensure_future(_watch_for_cancel(run_task))
            try:
                out = await run_task
            finally:
                watch_task.cancel()

            # Never surface a raw error/traceback as the chat answer — if the
            # orchestrator returned an error result, humanize it (friendly messages
            # are left untouched).
            try:
                from services.assistants.ask_job_callbacks import normalize_error_answer
                out = normalize_error_answer(out)
            except Exception:  # noqa: BLE001 — normalization must never break the turn
                pass

            try:
                try:
                    from services.assistants.ask_job_callbacks import steps_from_trace
                    turn_steps = steps_from_trace(out.get("trace"))
                except Exception:  # noqa: BLE001 — narration persistence is best-effort
                    turn_steps = []
                await store_assistant_message_cb(
                    thread_id=thread_id,
                    answer=out.get("answer") or "",
                    turn_id=None,
                    steps=turn_steps,
                )
            except Exception as store_exc:
                print(f"Failed to store assistant output message: {store_exc!r}")

            self.write_result(
                thread_id=thread_id,
                run_id=run_id,
                state="completed",
                result=out,
            )

            self.write_status(
                thread_id=thread_id,
                run_id=run_id,
                state="completed",
                extra={
                    "phase": "completed",
                    "message": "Request completed",
                    "mode": out.get("mode"),
                },
            )

        except asyncio.CancelledError:
            self.write_result(
                thread_id=thread_id,
                run_id=run_id,
                state="cancelled",
                result={"mode": "cancelled", "answer": "Request cancelled.", "thread_id": thread_id},
            )
            self.write_status(
                thread_id=thread_id,
                run_id=run_id,
                state="cancelled",
                extra={"phase": "cancelled", "message": "Request cancelled"},
            )
            raise

        except asyncio.TimeoutError:
            timeout_result = timeout_response_cb(thread_id)

            self.write_result(
                thread_id=thread_id,
                run_id=run_id,
                state="timed_out",
                result=timeout_result,
            )

            self.write_status(
                thread_id=thread_id,
                run_id=run_id,
                state="timed_out",
                extra={"phase": "timeout", "message": "Request timed out"},
            )

        except Exception as exc:
            error_result = error_response_cb(thread_id, exc)

            self.write_result(
                thread_id=thread_id,
                run_id=run_id,
                state="failed",
                result=error_result,
            )

            self.write_status(
                thread_id=thread_id,
                run_id=run_id,
                state="failed",
                extra={
                    "phase": "failed",
                    "message": str(exc),
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

        finally:
            self._running_tasks.pop(self._task_key(thread_id, run_id), None)
            _unregister_active_run(run_id)

    @staticmethod
    def _log_background_task_exception(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return

        if exc is not None:
            print(f"Ask job background task failed unexpectedly: {exc!r}")

    # ---------------------------------------------------------------------
    # Cleanup helpers
    # ---------------------------------------------------------------------

    def get_ask_run_age_reference(self, run_dir: Path, status: Dict[str, Any]) -> datetime:
        for key in ("completed_at", "updated_at", "created_at"):
            parsed = self.parse_iso_datetime(status.get(key))
            if parsed:
                return parsed

        request = self.read_json(run_dir / "request.json", default={})
        parsed = self.parse_iso_datetime(request.get("created_at"))
        if parsed:
            return parsed

        return self.path_mtime_utc(run_dir)

    def get_runtime_run_age_reference(self, run_dir: Path) -> datetime:
        """
        Generic age lookup for non-ask runtime jobs, such as voice jobs.

        It tries common metadata files first and then falls back to directory mtime.
        """
        for filename in ("status.json", "result.json", "request.json", "metadata.json", "job.json"):
            data = self.read_json(run_dir / filename, default={})

            for key in ("completed_at", "updated_at", "created_at", "started_at", "stopped_at"):
                parsed = self.parse_iso_datetime(data.get(key))
                if parsed:
                    return parsed

        return self.path_mtime_utc(run_dir)

    def get_runtime_run_state(self, run_dir: Path) -> Optional[str]:
        """
        Reads a state/status from common runtime metadata files.

        Voice jobs may not use the exact ask schema, so this method is lenient.
        """
        for filename in ("status.json", "result.json", "metadata.json", "job.json"):
            data = self.read_json(run_dir / filename, default={})
            state = data.get("state") or data.get("status") or data.get("phase")

            if isinstance(state, str) and state.strip():
                return state.strip().lower()

        return None

    def should_remove_runtime_run(
        self,
        *,
        state: Optional[str],
        reference_time: datetime,
        terminal_cutoff: datetime,
        active_cutoff: datetime,
    ) -> bool:
        normalized_state = (state or "").strip().lower()

        if normalized_state in TERMINAL_RUNTIME_STATES:
            return reference_time < terminal_cutoff

        if normalized_state in ACTIVE_RUNTIME_STATES:
            return reference_time < active_cutoff

        # Unknown/missing state: be conservative, but still clean old garbage.
        return reference_time < terminal_cutoff

    def cleanup_run_tree(
        self,
        *,
        root: Path,
        terminal_retention_hours: int,
        active_retention_hours: int,
        label: str,
    ) -> Dict[str, Any]:
        """
        Cleans a two-level runtime job tree:

            root/<thread_or_session_id>/<run_id>/

        The first subdirectory is treated as the thread/session directory.
        The second subdirectory is treated as the individual run directory.
        Empty first-level directories are removed after their runs are gone.
        """
        now = datetime.now(timezone.utc)
        terminal_cutoff = now - timedelta(hours=terminal_retention_hours)
        active_cutoff = now - timedelta(hours=active_retention_hours)

        removed_runs = 0
        removed_threads = 0
        errors = []

        if not root.exists():
            return {
                "label": label,
                "removed_runs": 0,
                "removed_threads": 0,
                "errors": [],
            }

        for thread_dir in root.iterdir():
            if not thread_dir.is_dir():
                continue

            for run_dir in thread_dir.iterdir():
                if not run_dir.is_dir():
                    continue

                try:
                    state = self.get_runtime_run_state(run_dir)
                    reference_time = self.get_runtime_run_age_reference(run_dir)

                    if self.should_remove_runtime_run(
                        state=state,
                        reference_time=reference_time,
                        terminal_cutoff=terminal_cutoff,
                        active_cutoff=active_cutoff,
                    ):
                        shutil.rmtree(run_dir)
                        removed_runs += 1

                except Exception as exc:
                    errors.append(
                        {
                            "label": label,
                            "run_dir": str(run_dir),
                            "error": repr(exc),
                        }
                    )

            try:
                if not any(thread_dir.iterdir()):
                    thread_dir.rmdir()
                    removed_threads += 1
            except Exception as exc:
                errors.append(
                    {
                        "label": label,
                        "thread_dir": str(thread_dir),
                        "error": repr(exc),
                    }
                )

        return {
            "label": label,
            "removed_runs": removed_runs,
            "removed_threads": removed_threads,
            "errors": errors,
        }

    def cleanup_old_jobs(self) -> Dict[str, Any]:
        ask_result = self.cleanup_run_tree(
            root=self.ask_root,
            terminal_retention_hours=self.run_retention_hours,
            active_retention_hours=self.active_retention_hours,
            label="ask",
        )

        voice_result = {
            "label": "voice",
            "removed_runs": 0,
            "removed_threads": 0,
            "errors": [],
        }

        if self.voice_root is not None:
            voice_result = self.cleanup_run_tree(
                root=self.voice_root,
                terminal_retention_hours=self.voice_retention_hours,
                active_retention_hours=self.voice_active_retention_hours,
                label="voice",
            )

        return {
            "removed_runs": ask_result["removed_runs"] + voice_result["removed_runs"],
            "removed_threads": ask_result["removed_threads"] + voice_result["removed_threads"],
            "errors": ask_result["errors"] + voice_result["errors"],
            "details": {
                "ask": ask_result,
                "voice": voice_result,
            },
        }

    async def cleanup_loop(self) -> None:
        """
        Optional standalone loop.

        If server.py registers cleanup_old_jobs with DynamicScheduler,
        you do not need to call this method.
        """
        while True:
            try:
                result = await asyncio.to_thread(self.cleanup_old_jobs)

                if result["removed_runs"] or result["removed_threads"] or result["errors"]:
                    print(f"Runtime cleanup result: {result}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Runtime cleanup failed: {exc!r}")

            await asyncio.sleep(self.cleanup_interval_seconds)
