from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from component.logging import get_logger


log = get_logger(__name__)


# Natural, user-facing labels for the internal system assistants. These surface
# in the live "thinking" trace, so they must read as plain human text — never the
# internal class name (e.g. "router_memory_retrieval_decision_assistant hop=0").
_ASSISTANT_TITLES = {
    "planner_memory_retrieval_decision_assistant": "Checking memories",
    "router_memory_retrieval_decision_assistant": "Checking memories",
    "cognition_router_system_assistant": "Routing the thought",
    "research_observation_system_assistant": "Reviewing observations",
    "belief_system_assistant": "Updating beliefs",
    "curiosity_system_assistant": "Forming a question",
    "research_system_assistant": "Researching",
    "memory_system_assistant": "Working with memories",
    "turn_interpretation_system_assistant": "Interpreting the conversation",
}


def _assistant_title(assistant) -> str:
    explicit = getattr(assistant, "title", None)
    if explicit:
        return str(explicit)
    name = (getattr(assistant, "name", "") or "").strip()
    if name in _ASSISTANT_TITLES:
        return _ASSISTANT_TITLES[name]
    words = name.replace("_assistant", "").replace("_system", "").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Thinking"


class SystemPipelineRunner:
    """
    Compact internal runner for System Assistants.

    Supported actions:
    - evaluate_answer: execute tool calls, then loop
    - finished: return final JSON

    This runner intentionally has no user-facing confirmation, pending actions,
    or ask_user mode.
    """

    def __init__(
        self,
        *,
        openai_service,
        tool_runner,
        trace_fn: Optional[Callable[..., None]] = None,
        max_hops: int = 4,
        default_model: Optional[str] = None,  # None → resolved from the chat.cognition slot
    ):
        log.infox(
            "SystemPipelineRunner initialiseren",
            has_openai_service=openai_service is not None,
            has_tool_runner=tool_runner is not None,
            has_trace_fn=trace_fn is not None,
            max_hops=max_hops,
            default_model=default_model,
        )
        self.openai = openai_service
        self.tool_runner = tool_runner
        self.trace_fn = trace_fn
        self.max_hops = max_hops
        self.default_model = default_model
        log.debugx(
            "SystemPipelineRunner geïnitialiseerd",
            max_hops=self.max_hops,
            default_model=self.default_model,
        )

    async def run(
        self,
        *,
        assistant,
        prompt_kwargs: Dict[str, Any],
        session_id: Optional[str],
        turn_id: int,
        trace: Optional[List[dict]] = None,
        model: Optional[str] = None,
        role: Optional[str] = None,
        progress_cb=None,
    ) -> Dict[str, Any]:
        log.infox(
            "System pipeline run gestart",
            assistant_name=getattr(assistant, "name", None),
            session_id=session_id,
            turn_id=turn_id,
            prompt_kwargs_keys=list(prompt_kwargs.keys()) if isinstance(prompt_kwargs, dict) else None,
            trace_count=len(trace or []),
            model=model,
            default_model=self.default_model,
            selected_model=model or self.default_model,
            max_hops=self.max_hops,
            has_progress_cb=progress_cb is not None,
        )
        trace = trace or []
        accumulated_tool_results: List[Dict[str, Any]] = []
        accumulated_tool_calls: List[Dict[str, Any]] = []

        for hop in range(self.max_hops):
            log.infox(
                "System pipeline hop gestart",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                max_hops=self.max_hops,
                accumulated_tool_call_count=len(accumulated_tool_calls),
                accumulated_tool_result_count=len(accumulated_tool_results),
            )
            current_kwargs = dict(prompt_kwargs)
            current_kwargs["previous_tool_results"] = {
                "tool_calls": accumulated_tool_calls,
                "tool_results": accumulated_tool_results,
            }

            log.debugx(
                "System assistant prompt bouwen gestart",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                current_kwargs_keys=list(current_kwargs.keys()),
            )
            prompt = assistant.prompt(**current_kwargs)
            log.debugx(
                "System assistant prompt gebouwd",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                prompt_length=len(prompt or ""),
            )

            self._trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="system_assistant.openai.start",
                summary=f"{_assistant_title(assistant)}…",
                data={"assistant": assistant.name, "hop": hop},
                progress_cb=progress_cb,
            )

            log.infox(
                "System assistant OpenAI call gestart",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                model=model or self.default_model,
                keep_context=False,
            )
            t0 = time.time()
            # Async + role-tagged so the call (a) never blocks the event loop and
            # (b) routes via the registry (chat.cognition / chat.memory_decision
            # slots, else the agnostic default) instead of always hitting OpenAI.
            effective_role = role or f"cognition:{getattr(assistant, 'name', 'system')}"
            resp = await self.openai.ask_orchestration_async(
                prompt,
                role=effective_role,
                instructions=assistant.instructions,
                keep_context=False,
                store=False,
                session_id=None,
                model=model or self.default_model,
                json_schema=getattr(getattr(assistant, "config", None), "schema", None),
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            log.infox(
                "System assistant OpenAI call afgerond",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                elapsed_ms=elapsed_ms,
                response_text_length=len(getattr(resp, "text", "") or ""),
            )

            self._trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="system_assistant.openai.end",
                summary=_assistant_title(assistant),
                data={
                    "assistant": assistant.name,
                    "hop": hop,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
                progress_cb=progress_cb,
            )

            log.debugx(
                "System assistant JSON plan extractie gestart",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                response_text_length=len(getattr(resp, "text", "") or ""),
            )
            plan = assistant.extract_first_json_object(resp.text)
            if not isinstance(plan, dict):
                log.warningx(
                    "System assistant gaf ongeldige JSON terug",
                    assistant_name=getattr(assistant, "name", None),
                    session_id=session_id,
                    turn_id=turn_id,
                    hop=hop,
                    raw_length=len(getattr(resp, "text", "") or ""),
                    accumulated_tool_call_count=len(accumulated_tool_calls),
                    accumulated_tool_result_count=len(accumulated_tool_results),
                )
                return {
                    "ok": False,
                    "error": "System assistant returned invalid JSON.",
                    "assistant": assistant.name,
                    "raw": resp.text,
                    "tool_calls": accumulated_tool_calls,
                    "tool_results": accumulated_tool_results,
                }

            log.debugx(
                "System assistant JSON plan geëxtraheerd",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                plan_keys=list(plan.keys()),
            )

            action = (plan.get("action") or "").strip()
            log.infox(
                "System assistant plan ontvangen",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                action=action,
                plan_keys=list(plan.keys()),
            )

            self._trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="system_assistant.plan",
                summary=_assistant_title(assistant),
                data={"assistant": assistant.name, "action": action, "plan": plan},
                progress_cb=progress_cb,
            )

            if action == "finished":
                log.infox(
                    "System pipeline afgerond met action finished",
                    assistant_name=getattr(assistant, "name", None),
                    session_id=session_id,
                    turn_id=turn_id,
                    hop=hop,
                    accumulated_tool_call_count=len(accumulated_tool_calls),
                    accumulated_tool_result_count=len(accumulated_tool_results),
                )
                return {
                    "ok": True,
                    "assistant": assistant.name,
                    "result": plan,
                    "tool_calls": accumulated_tool_calls,
                    "tool_results": accumulated_tool_results,
                }

            if action != "evaluate_answer":
                log.warningx(
                    "System assistant action wordt niet ondersteund",
                    assistant_name=getattr(assistant, "name", None),
                    session_id=session_id,
                    turn_id=turn_id,
                    hop=hop,
                    action=action,
                    supported_actions=["evaluate_answer", "finished"],
                )
                return {
                    "ok": False,
                    "error": f"Unsupported system assistant action: {action}",
                    "assistant": assistant.name,
                    "plan": plan,
                    "tool_calls": accumulated_tool_calls,
                    "tool_results": accumulated_tool_results,
                }

            tool_calls = plan.get("tool_calls") or []
            log.debugx(
                "System assistant evaluate_answer tool calls ontvangen",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                tool_calls_type=type(tool_calls).__name__,
                tool_call_count=len(tool_calls) if isinstance(tool_calls, list) else None,
            )
            if not isinstance(tool_calls, list) or not tool_calls:
                log.warningx(
                    "System assistant evaluate_answer zonder geldige tool_calls",
                    assistant_name=getattr(assistant, "name", None),
                    session_id=session_id,
                    turn_id=turn_id,
                    hop=hop,
                    tool_calls_type=type(tool_calls).__name__,
                )
                return {
                    "ok": False,
                    "error": "evaluate_answer requires non-empty tool_calls.",
                    "assistant": assistant.name,
                    "plan": plan,
                    "tool_calls": accumulated_tool_calls,
                    "tool_results": accumulated_tool_results,
                }

            log.infox(
                "System assistant tool calls uitvoeren gestart",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                tool_call_count=len(tool_calls),
                tool_names=[tc.get("tool") for tc in tool_calls if isinstance(tc, dict)],
            )
            tool_results = await self.tool_runner.execute_tool_calls(
                tool_calls=tool_calls,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
                assistant_name=assistant.name,
                trace_fn=self._trace,
                preview_fn=lambda x: str(x)[:500],
                progress_cb=progress_cb,
            )
            log.infox(
                "System assistant tool calls uitvoeren afgerond",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                tool_call_count=len(tool_calls),
                tool_result_count=len(tool_results) if tool_results is not None else None,
            )

            accumulated_tool_calls.extend(tool_calls)
            accumulated_tool_results.extend(tool_results)
            log.debugx(
                "System assistant tool calls/resultaten geaccumuleerd",
                assistant_name=getattr(assistant, "name", None),
                session_id=session_id,
                turn_id=turn_id,
                hop=hop,
                accumulated_tool_call_count=len(accumulated_tool_calls),
                accumulated_tool_result_count=len(accumulated_tool_results),
            )

        log.warningx(
            "System assistant heeft max_hops overschreden",
            assistant_name=getattr(assistant, "name", None),
            session_id=session_id,
            turn_id=turn_id,
            max_hops=self.max_hops,
            accumulated_tool_call_count=len(accumulated_tool_calls),
            accumulated_tool_result_count=len(accumulated_tool_results),
        )
        return {
            "ok": False,
            "error": f"System assistant exceeded max_hops={self.max_hops}.",
            "assistant": assistant.name,
            "tool_calls": accumulated_tool_calls,
            "tool_results": accumulated_tool_results,
        }

    def _trace(
            self,
            trace,
            *,
            thread_id,
            turn_id,
            type,
            summary,
            data,
            level="info",
            progress_cb=None,
            **kwargs,
    ):
        log.debugx(
            "System pipeline trace event verwerken",
            thread_id=thread_id,
            turn_id=turn_id,
            type=type,
            level=level,
            summary=summary,
            has_trace_fn=self.trace_fn is not None,
            trace_count=len(trace) if trace is not None else None,
            data_keys=list((data or {}).keys()) if isinstance(data or {}, dict) else None,
            has_progress_cb=progress_cb is not None,
        )
        if self.trace_fn:
            self.trace_fn(
                trace,
                thread_id=thread_id,
                turn_id=turn_id,
                type=type,
                level=level,
                summary=summary,
                data=data or {},
                progress_cb=progress_cb,
            )
            log.debugx(
                "System pipeline trace event verwerkt via trace_fn",
                thread_id=thread_id,
                turn_id=turn_id,
                type=type,
                level=level,
            )
        else:
            trace.append({
                "ts": time.time(),
                "turn_id": turn_id,
                "seq": len(trace),
                "type": type,
                "level": level,
                "summary": summary,
                "data": data or {},
            })
            log.debugx(
                "System pipeline trace event toegevoegd aan lokale trace",
                thread_id=thread_id,
                turn_id=turn_id,
                type=type,
                level=level,
                trace_count=len(trace),
            )