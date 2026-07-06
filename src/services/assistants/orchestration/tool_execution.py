from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi.encoders import jsonable_encoder

from component.config import settings
from component.logging import get_logger
from services.assistants.orchestration.placeholder import resolve_placeholders
from services.assistants.orchestration.tool_result_artifacts import ToolResultNormalizer
from services.assistants.orchestration.guarded_tools import is_guarded_tool, tool_call_hash


log = get_logger(__name__)

TraceFn = Callable[..., None]
PreviewFn = Callable[[Any], Any]


def _tool_call_id(tc: Dict[str, Any]) -> Optional[int]:
    log.debugx(
        "Tool call id ophalen gestart",
        tc_keys=list(tc.keys()) if isinstance(tc, dict) else None,
        raw_tool_id=tc.get("tool_id") if isinstance(tc, dict) else None,
    )
    value = tc.get("tool_id")
    if value is None:
        log.debugx("Tool call id ontbreekt")
        return None
    try:
        result = int(value)
        log.debugx("Tool call id ophalen afgerond", raw_tool_id=value, tool_id=result)
        return result
    except (TypeError, ValueError):
        log.warningx("Tool call id ongeldig", raw_tool_id=value, raw_type=type(value).__name__)
        return None


# Legacy single-underscore aliases for the builtin text tools. Some skill instructions
# (and older code) refer to e.g. `text_search`, but the real builtin tools are
# double-underscore (`text__search`). Canonicalise so the agent calling either name
# resolves to the actual tool (the guard + execution match on the real name).
_TOOL_NAME_ALIASES: Dict[str, str] = {
    "text_search": "text__search",
    "text_ingest": "text__ingest",
    "text_ingest_status": "text__ingest_status",
    "text_ingest_wait": "text__ingest_wait",
    "text_update": "text__update",
    "text_delete": "text__delete",
    "text_get_file": "text__get_file",
    "text_list_files": "text__list_files",
}


def canonical_tool_name(name: str) -> str:
    """Map legacy single-underscore tool names to their real builtin name."""
    return _TOOL_NAME_ALIASES.get((name or "").strip(), (name or "").strip())


def _tool_call_name(tc: Dict[str, Any]) -> str:
    result = canonical_tool_name(tc.get("tool") or "")
    log.debugx("Tool call naam bepaald", tool=result, tc_keys=list(tc.keys()) if isinstance(tc, dict) else None)
    return result


def _args_reference_prior_results(args: Any) -> bool:
    """True als args een ${...} placeholder bevatten.

    Placeholders (bijv. ${result.0.id} of ${last.items}) verwijzen naar de
    resultaten van eerdere tool calls in dezelfde turn. Zulke calls moeten
    serieel ná hun afhankelijkheden draaien en mogen niet parallel.
    """
    try:
        serialized = json.dumps(args, default=str)
    except Exception:
        serialized = str(args)
    return "${" in serialized


def _truncate_summary(text: Any, limit: int = 120) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _call_is_parallel_eligible(tool: str, args: Any) -> bool:
    """Een tool call mag parallel draaien als hij niet guarded is én geen
    placeholder-afhankelijkheid heeft naar eerdere resultaten. Dit garandeert
    semantische gelijkwaardigheid met sequentiële uitvoering.
    """
    if is_guarded_tool(tool):
        return False
    if _args_reference_prior_results(args):
        return False
    return True


class ToolExecutionRunner:
    def __init__(
        self,
        *,
        tool_execution_service,
        ingest_wait_timeout_s: float,
        ingest_poll_interval_s: float,
        max_tool_calls_per_turn: int,
    ):
        log.infox(
            "ToolExecutionRunner initialiseren",
            has_tool_execution_service=tool_execution_service is not None,
            ingest_wait_timeout_s=ingest_wait_timeout_s,
            ingest_poll_interval_s=ingest_poll_interval_s,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
        )
        self.tool_execution_service = tool_execution_service
        self.ingest_wait_timeout_s = ingest_wait_timeout_s
        self.ingest_poll_interval_s = ingest_poll_interval_s
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        log.infox(
            "ToolExecutionRunner geïnitialiseerd",
            ingest_wait_timeout_s=self.ingest_wait_timeout_s,
            ingest_poll_interval_s=self.ingest_poll_interval_s,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )

    async def wait_for_ingest_job(self, job_id: str) -> Dict[str, Any]:
        from services.builtin.internal_tool_registry import internal_tool_registry

        log.infox(
            "Wachten op ingest job gestart",
            job_id=job_id,
            timeout_s=self.ingest_wait_timeout_s,
            poll_interval_s=self.ingest_poll_interval_s,
        )
        deadline = time.time() + float(self.ingest_wait_timeout_s)
        poll_count = 0
        while True:
            if time.time() >= deadline:
                log.warningx(
                    "Wachten op ingest job timeout",
                    job_id=job_id,
                    poll_count=poll_count,
                    timeout_s=self.ingest_wait_timeout_s,
                )
                return {"status": "timeout", "job_id": job_id}

            poll_count += 1
            log.debugx("Ingest job status ophalen", job_id=job_id, poll_count=poll_count)
            st = await internal_tool_registry.call("text__ingest_status", {"job_id": job_id})
            log.debugx(
                "Ingest job status ontvangen",
                job_id=job_id,
                poll_count=poll_count,
                status=st.get("status") if isinstance(st, dict) else None,
            )
            if isinstance(st, dict) and st.get("status") in ("done", "error"):
                log.infox("Wachten op ingest job afgerond", job_id=job_id, status=st.get("status"), poll_count=poll_count)
                return st

            await asyncio.sleep(self.ingest_poll_interval_s)

    async def execute_dynamic_tool_call(self, tool_id: int, args: Dict[str, Any]) -> Any:
        log.infox("Dynamic tool call uitvoeren gestart", tool_id=tool_id, arg_keys=list(args.keys()) if isinstance(args, dict) else None)
        result = await self.tool_execution_service.execute_tool(tool_id=tool_id, args=args)
        log.infox("Dynamic tool call uitvoeren afgerond", tool_id=tool_id, result_type=type(result).__name__)
        return result

    async def call_tool_with_ingest_handling(self, tool_call: Dict[str, Any], args: Dict[str, Any]) -> Any:
        from services.builtin.internal_tool_registry import internal_tool_registry

        tool_name = (tool_call.get("tool") or "").strip()
        tool_id = _tool_call_id(tool_call)

        log.infox(
            "Tool call met ingest handling gestart",
            tool=tool_name,
            tool_id=tool_id,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
        )

        # text__ingest via internal tool registry
        if tool_name == "text__ingest":
            log.infox("Internal text__ingest call gestart", tool=tool_name, arg_keys=list(args.keys()) if isinstance(args, dict) else None)
            out = await internal_tool_registry.call("text__ingest", args)
            log.infox("Internal text__ingest call afgerond", tool=tool_name, output_type=type(out).__name__, status=out.get("status") if isinstance(out, dict) else None)
            if isinstance(out, dict) and out.get("status") == "done":
                return out
            if isinstance(out, dict) and out.get("status") == "queued" and out.get("job_id"):
                log.infox("text__ingest queued, wachten op job", job_id=out.get("job_id"))
                return await self.wait_for_ingest_job(out["job_id"])
            return out

        # text__ingest_status via internal tool registry
        if tool_name == "text__ingest_status":
            log.infox("Internal text__ingest_status call gestart", tool=tool_name)
            result = await internal_tool_registry.call("text__ingest_status", args)
            log.infox("Internal text__ingest_status call afgerond", tool=tool_name)
            return result

        # Internal capability tools (agent__dispatch, task__*, …) have no DB id and
        # execute by NAME via the internal registry. The agent is required to send a
        # tool_id, but these use a 0-sentinel — so route any internal tool whose id is
        # absent/non-positive by name. A genuine dynamic tool without a real id is an
        # error (hard-stop, caught upstream as a terminal failure).
        has_real_id = isinstance(tool_id, int) and not isinstance(tool_id, bool) and tool_id > 0
        if not has_real_id:
            if internal_tool_registry.has_tool(tool_name):
                log.infox("Internal tool op naam uitvoeren", tool=tool_name)
                return await internal_tool_registry.call(tool_name, args)
            log.warningx("Dynamic tool call mist tool_id", tool=tool_name)
            raise ValueError(f"Missing tool_id for dynamic tool call: tool={tool_name!r}")

        result = await self.execute_dynamic_tool_call(tool_id, args)
        log.infox("Tool call met ingest handling afgerond via dynamic tool", tool=tool_name, tool_id=tool_id)
        return result

    def normalize_tool_calls(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        log.infox(
            "Tool calls normaliseren gestart",
            plan_type=type(plan).__name__,
            plan_keys=list(plan.keys()) if isinstance(plan, dict) else None,
            has_tool_calls=isinstance(plan.get("tool_calls") if isinstance(plan, dict) else None, list),
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )
        if isinstance(plan.get("tool_calls"), list):
            calls = [c for c in plan["tool_calls"] if isinstance(c, dict)]
            log.debugx(
                "Tool calls uit plan.tool_calls gelezen",
                input_count=len(plan.get("tool_calls") or []),
                dict_call_count=len(calls),
                skipped_count=len(plan.get("tool_calls") or []) - len(calls),
            )
        else:
            tool = plan.get("tool")
            tool_id = plan.get("tool_id")
            kind = plan.get("kind")
            args = plan.get("args") or {}
            calls = [{
                "tool_id": tool_id,
                "tool": tool,
                "kind": kind,
                "args": args,
                "reason": "legacy_single_tool",
            }] if (tool or tool_id is not None) else []
            log.debugx(
                "Legacy single tool call normalisatie gebruikt",
                tool=tool, tool_id=tool_id, kind=kind,
                has_args=bool(args), call_count=len(calls),
            )

        normalized: List[Dict[str, Any]] = []
        for c in calls[: int(self.max_tool_calls_per_turn)]:
            normalized_call = {
                "tool_id": _tool_call_id(c),
                "tool": canonical_tool_name(c.get("tool") or ""),
                "kind": (c.get("kind") or "").strip(),
                "args": c.get("args") or {},
                "reason": c.get("reason"),
            }
            normalized.append(normalized_call)
            log.debugx(
                "Tool call genormaliseerd",
                tool=normalized_call.get("tool"),
                tool_id=normalized_call.get("tool_id"),
                kind=normalized_call.get("kind"),
                arg_keys=list(normalized_call.get("args").keys()) if isinstance(normalized_call.get("args"), dict) else None,
                reason=normalized_call.get("reason"),
            )

        log.infox(
            "Tool calls normaliseren afgerond",
            input_call_count=len(calls),
            normalized_call_count=len(normalized),
            truncated=len(calls) > int(self.max_tool_calls_per_turn),
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            tool_names=[c.get("tool") for c in normalized],
        )
        return normalized

    async def _run_single_tool_call(
        self,
        *,
        tc: Dict[str, Any],
        results: List[Any],
        session_id: Optional[str],
        turn_id: int,
        trace: List[dict],
        assistant_name: str,
        trace_fn: TraceFn,
        preview_fn: PreviewFn,
        progress_cb=None,
        confirmed_tool_call_hashes: Optional[set[str]] = None,
    ) -> Any:
        """Voer één enkele tool call uit en retourneer het genormaliseerde
        resultaat. ``results`` is de (gedeeltelijk gevulde) resultatenlijst van
        de turn en wordt enkel gebruikt om placeholders op te lossen; deze
        helper schrijft er zelf niet in (de caller plaatst het resultaat op de
        juiste index om volgorde-/index-uitlijning te bewaren).
        """
        tool = (tc.get("tool") or "").strip()
        tool_id = _tool_call_id(tc)
        args = tc.get("args") or {}

        log.infox(
            "Tool call voorbereiden",
            session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
            tool=tool, tool_id=tool_id,
            raw_arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            current_result_count=len([r for r in results if r is not None]),
        )

        args = resolve_placeholders(args, results=results)
        effective_tc = {**tc, "args": args}

        if is_guarded_tool(tool):
            current_hash = tool_call_hash(effective_tc)
            if current_hash not in (confirmed_tool_call_hashes or set()):
                trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="guarded_tool_confirmation_required",
                    level="error",
                    summary="Guarded tool blocked before execution",
                    data={
                        "assistant": assistant_name,
                        "tool": tool,
                        "tool_id": tool_id,
                        "tool_call_hash": current_hash,
                        "confirmed": False,
                    },
                    progress_cb=progress_cb,
                )
                raise PermissionError(f"Guarded tool {tool!r} requires confirmation before execution.")

        log.debugx(
            "Tool call placeholders resolved",
            session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
            tool=tool, tool_id=tool_id,
            resolved_arg_keys=list(args.keys()) if isinstance(args, dict) else None,
        )

        trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="tool_call",
            summary=f"Calling {tool or tool_id}",
            data={"assistant": assistant_name, "tool": tool, "tool_id": tool_id, "args": args},
            progress_cb=progress_cb,
        )

        # Internal tools en dynamic tools met tool_id mogen altijd — alleen
        # dynamic tools zonder een ECHTE tool_id én zonder internal registry match
        # worden geblokkeerd. Een echte id is een positief geheel getal; internal
        # capability-tools (agent__dispatch, task__*) sturen een 0-sentinel omdat ze
        # geen DB-id hebben en op naam worden uitgevoerd.
        from services.builtin.internal_tool_registry import internal_tool_registry
        is_internal = internal_tool_registry.has_tool(tool)
        has_real_tool_id = isinstance(tool_id, int) and not isinstance(tool_id, bool) and tool_id > 0

        if not is_internal and not has_real_tool_id:
            log.warningx(
                "Tool call geblokkeerd: dynamic tool zonder tool_id",
                session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                tool=tool, args_keys=list(args.keys()) if isinstance(args, dict) else None,
            )
            trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="error",
                summary=f"Planner returned a dynamic tool call without tool_id for tool {tool or tool_id}",
                data={"assistant": assistant_name, "tool": tool, "args": args},
                progress_cb=progress_cb,
            )
            raise ValueError(f"Planner returned a dynamic tool call without tool_id for tool={tool!r}.")

        # Subagent-dispatch wordt als gewone tool uitgevoerd, maar krijgt een
        # eigen trace-event zodat delegatie helder zichtbaar is in de timeline.
        is_subagent_dispatch = tool == "agent__dispatch"
        if is_subagent_dispatch:
            trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="subagent_spawned",
                summary=f"Dispatching subagent: {(args.get('assistant') or 'ad-hoc')}",
                data={
                    "assistant": assistant_name,
                    "subagent_assistant": args.get("assistant") or "ad-hoc",
                    "task": args.get("task"),
                },
                progress_cb=progress_cb,
            )

        t0 = time.time()
        try:
            log.infox(
                "Tool call uitvoeren",
                session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                tool=tool, tool_id=tool_id,
            )
            result = await self.call_tool_with_ingest_handling(tc, args)
            log.infox(
                "Tool call succesvol uitgevoerd",
                session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                tool=tool, tool_id=tool_id,
                result_type=type(result).__name__,
                result_keys=list(result.keys()) if isinstance(result, dict) else None,
            )
        except Exception as e:
            log.exceptionx(
                "Tool call uitvoeren mislukt",
                session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                tool=tool, tool_id=tool_id, exception=e,
            )
            result = {"error": f"{type(e).__name__}: {e}", "tool": tool, "args": args}

        dt_ms = int((time.time() - t0) * 1000)
        raw_result = jsonable_encoder(result)
        normalized = ToolResultNormalizer(thread_id=session_id, run_id=str(turn_id)).normalize(tool_call=tc, raw_result=raw_result)

        log.infox(
            "Tool result opgeslagen",
            session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
            tool=tool, tool_id=tool_id, duration_ms=dt_ms,
            result_type=type(normalized).__name__,
            result_keys=list(normalized.keys()) if isinstance(normalized, dict) else None,
        )

        trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="tool_result",
            summary=f"{tool} returned in {dt_ms}ms",
            data={
                "assistant": assistant_name,
                "tool": tool, "args": args,
                "status": normalized.get("status"),
                "inspection_level": normalized.get("inspection_level"),
                "truncated": normalized.get("truncated"),
                "size_bytes": normalized.get("size_bytes"),
                "content_ref": normalized.get("content_ref"),
                "local_path": normalized.get("local_path"),
                "summary": normalized.get("summary"),
                "artifacts": normalized.get("artifacts"),
                "result_preview": preview_fn({k: normalized.get(k) for k in ("status","tool","summary","inspection_level","truncated","content_ref")}),
                "duration_ms": dt_ms,
            },
            progress_cb=progress_cb,
        )

        if is_subagent_dispatch:
            res = normalized if isinstance(normalized, dict) else {}
            trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="subagent_completed",
                level="error" if str(res.get("status")).lower() == "error" else "info",
                summary=f"Subagent finished: {_truncate_summary(res.get('summary'))}",
                data={
                    "assistant": assistant_name,
                    "subagent_thread_id": res.get("thread_id"),
                    "status": res.get("status"),
                    "terminal_state": res.get("terminal_state"),
                    "summary": res.get("summary"),
                    "open_questions": res.get("open_questions"),
                    "artifacts": res.get("artifacts"),
                },
                progress_cb=progress_cb,
            )

        return normalized

    async def execute_tool_calls(
        self,
        *,
        tool_calls: List[Dict[str, Any]],
        session_id: Optional[str],
        turn_id: int,
        trace: List[dict],
        assistant_name: str,
        trace_fn: TraceFn,
        preview_fn: PreviewFn,
        progress_cb=None,
        confirmed_tool_call_hashes: Optional[set[str]] = None,
    ) -> List[Any]:
        max_parallel = max(1, int(getattr(settings, "MAX_PARALLEL_TOOL_CALLS", 5)))
        log.infox(
            "Tool calls uitvoeren gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            tool_call_count=len(tool_calls or []),
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            max_parallel=max_parallel,
        )

        calls = list(tool_calls or [])
        n = len(calls)
        # Resultaten worden op index geplaatst zodat ${result.N} placeholders
        # consistent blijven met sequentiële uitvoering, ook bij parallelisme.
        tool_results: List[Any] = [None] * n

        async def run_at(index: int, semaphore: Optional[asyncio.Semaphore] = None) -> None:
            if semaphore is not None:
                async with semaphore:
                    normalized = await self._run_single_tool_call(
                        tc=calls[index], results=tool_results,
                        session_id=session_id, turn_id=turn_id, trace=trace,
                        assistant_name=assistant_name, trace_fn=trace_fn, preview_fn=preview_fn,
                        progress_cb=progress_cb, confirmed_tool_call_hashes=confirmed_tool_call_hashes,
                    )
            else:
                normalized = await self._run_single_tool_call(
                    tc=calls[index], results=tool_results,
                    session_id=session_id, turn_id=turn_id, trace=trace,
                    assistant_name=assistant_name, trace_fn=trace_fn, preview_fn=preview_fn,
                    progress_cb=progress_cb, confirmed_tool_call_hashes=confirmed_tool_call_hashes,
                )
            tool_results[index] = normalized

        i = 0
        while i < n:
            tc = calls[i]
            tool = (tc.get("tool") or "").strip()
            args = tc.get("args") or {}

            # Verzamel een aaneengesloten reeks parallel-geschikte calls.
            batch: List[int] = []
            if max_parallel > 1 and _call_is_parallel_eligible(tool, args):
                while i < n:
                    tcj = calls[i]
                    if _call_is_parallel_eligible((tcj.get("tool") or "").strip(), tcj.get("args") or {}):
                        batch.append(i)
                        i += 1
                    else:
                        break

            if len(batch) > 1:
                semaphore = asyncio.Semaphore(max_parallel)
                trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="parallel_tool_batch",
                    summary=f"Running {len(batch)} tool calls in parallel",
                    data={
                        "assistant": assistant_name,
                        "count": len(batch),
                        "max_parallel": max_parallel,
                        "tools": [(calls[idx].get("tool") or "").strip() for idx in batch],
                    },
                    progress_cb=progress_cb,
                )
                await asyncio.gather(*[run_at(idx, semaphore) for idx in batch])
            elif len(batch) == 1:
                await run_at(batch[0])
            else:
                # Niet parallel-geschikt (guarded of placeholder-afhankelijk): serieel.
                await run_at(i)
                i += 1

        log.infox(
            "Tool calls uitvoeren afgerond",
            session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
            tool_call_count=n,
            tool_result_count=len([r for r in tool_results if r is not None]),
        )
        return tool_results