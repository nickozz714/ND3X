from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from fastapi.encoders import jsonable_encoder

from component.logging import get_logger
from services.assistants.orchestration.placeholder import resolve_placeholders
from services.system_tools.research_tool import ResearchTool


log = get_logger(__name__)

TraceFn = Callable[..., None]
PreviewFn = Callable[[Any], Any]


class SystemToolExecutionRunner:
    """
    Minimal internal tool runner for System Assistants.

    No dynamic tool IDs.
    No MCP fallback.
    No mutation confirmation.
    No assistant registry.
    No user-facing tools.

    Tools are addressed by stable internal names.
    """

    def __init__(self):
        log.debugx("SystemToolExecutionRunner initialiseren")
        self.tools = {
            # Provider-aware web research (keyless DuckDuckGo by default, Exa when
            # a key is set). Kept under the "exa_research" name for the assistants.
            "exa_research": ResearchTool(),
        }
        log.infox(
            "SystemToolExecutionRunner geïnitialiseerd",
            tool_names=list(self.tools.keys()),
            tool_count=len(self.tools),
        )

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
    ) -> List[Any]:
        log.infox(
            "System tool calls uitvoeren gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            tool_call_count=len(tool_calls or []),
            trace_count=len(trace or []),
            has_trace_fn=trace_fn is not None,
            has_preview_fn=preview_fn is not None,
            has_progress_cb=progress_cb is not None,
        )

        results: List[Any] = []

        for tc in tool_calls or []:
            log.debugx(
                "System tool call verwerken gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                raw_tool_call_keys=list(tc.keys()) if isinstance(tc, dict) else None,
                current_result_count=len(results),
            )

            tool_name = (tc.get("tool") or "").strip()
            args = tc.get("args") or {}
            log.debugx(
                "System tool call gegevens gelezen",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool=tool_name,
                arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            )

            args = resolve_placeholders(args, results=results)
            log.debugx(
                "System tool call placeholders opgelost",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool=tool_name,
                arg_keys=list(args.keys()) if isinstance(args, dict) else None,
                previous_result_count=len(results),
            )

            log.infox(
                "System tool call starten",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool=tool_name,
                arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            )

            trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="system_tool_call",
                summary=f"Calling system tool {tool_name}",
                data={
                    "assistant": assistant_name,
                    "tool": tool_name,
                    "args": args,
                },
                progress_cb=progress_cb,
            )

            t0 = time.time()

            try:
                tool = self.tools.get(tool_name)
                if not tool:
                    log.warningx(
                        "System tool call mislukt: onbekende tool",
                        session_id=session_id,
                        turn_id=turn_id,
                        assistant_name=assistant_name,
                        tool=tool_name,
                        available_tools=list(self.tools.keys()),
                    )
                    raise ValueError(f"Unknown system tool: {tool_name!r}")

                log.debugx(
                    "System tool gevonden, run uitvoeren",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    tool=tool_name,
                    tool_type=type(tool).__name__,
                )
                result = await tool.run(**args)
                log.infox(
                    "System tool run succesvol afgerond",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    tool=tool_name,
                    result_type=type(result).__name__,
                    ok=result.get("ok") if isinstance(result, dict) else None,
                )
            except Exception as e:
                log.errorx(
                    "System tool call gaf fout terug",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    tool=tool_name,
                    error=f"{type(e).__name__}: {e}",
                )
                result = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "tool": tool_name,
                    "args": args,
                }

            duration_ms = int((time.time() - t0) * 1000)
            log.debugx(
                "System tool result jsonable encoden gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool=tool_name,
                duration_ms=duration_ms,
                result_type=type(result).__name__,
            )
            result = jsonable_encoder(result)
            results.append(result)
            log.infox(
                "System tool call afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool=tool_name,
                duration_ms=duration_ms,
                result_type=type(result).__name__,
                result_count=len(results),
                ok=result.get("ok") if isinstance(result, dict) else None,
            )

            trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="system_tool_result",
                summary=f"System tool {tool_name} returned in {duration_ms}ms",
                data={
                    "assistant": assistant_name,
                    "tool": tool_name,
                    "duration_ms": duration_ms,
                    "result_preview": preview_fn(result),
                },
                progress_cb=progress_cb,
            )

        log.infox(
            "System tool calls uitvoeren afgerond",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            result_count=len(results),
        )
        return results