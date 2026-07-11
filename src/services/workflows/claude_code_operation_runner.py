"""
services/workflows/claude_code_operation_runner.py

Alternative execution engine for an assistant workflow activity: run the step
as one AUTONOMOUS Claude Code CLI task, fully outside the ND3X orchestrator
(the CLI drives its own multi-turn agent loop with its own tools), instead of
the ND3X agent loop with ND3X tools.

The step still plugs into the workflow the same way: this runner returns a
`result` dict shaped exactly like a pipeline result (mode/answer/trace/
downstream_handoff), so the executor's existing post-processing — the
success/partial/failed handoff gate, For-Each `iterables`, on_success/
on_failure follow-ups, run transcript — all work unchanged. The handoff is the
"tool that hands the result back / continues the workflow": we require the CLI
to end its run with one JSON envelope carrying the answer + downstream_handoff.

Engine selection lives in the operation's `config.execution`:
  {"engine": "claude_code",           # else the orchestrator engine runs
   "allowed_tools": "Bash Read Edit", # optional per-step tool allowlist
   "max_turns": 30, "timeout": 1800}
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from services.providers.registry_service import ProviderRegistryService

log = get_logger(__name__)

def _handoff_instruction() -> str:
    """Workflow-step instruction: the shared ND3X world-context + the handoff
    tail. Shares ND3X_AGENT_PREAMBLE with the chat agent so the workflow step
    also knows ND3X's tools live under mcp__nd3x, respects the host-boundary and
    replies in the user's language — the exact context the chat agent gained."""
    from services.providers.claude_code_provider import ND3X_AGENT_PREAMBLE
    return ND3X_AGENT_PREAMBLE + "\n\n" + _HANDOFF_TAIL


_HANDOFF_TAIL = (
    "You are running as one autonomous step inside an ND3X workflow (no user is "
    "watching — never ask a question; build on the earlier steps' outcomes given "
    "to you).\n\n"
    "When finished, your LAST message must be a single JSON object and nothing "
    "else, matching:\n"
    "{\n"
    '  "answer": "<natural-language result of this step>",\n'
    '  "downstream_handoff": {\n'
    '    "summary": "<concise outcome>",\n'
    '    "full_answer": "<full result for later synthesis, or null>",\n'
    '    "artifacts": [ {"path": "...", "title": "...", "status": "created|updated|deleted"} ],\n'
    '    "facts": { },\n'
    '    "iterables": { },\n'
    '    "open_questions": [ ],\n'
    '    "status": "success | partial | failed"\n'
    "  }\n"
    "}\n"
    "Rules: perform any required create/update/delete BEFORE reporting success. "
    "Use status='partial' if work remains, 'failed' if the required outcome was "
    "not achieved. Keep the handoff compact — no raw tool dumps. Populate "
    "iterables only when a later For-Each step should fan out over your results; "
    "each value is an array of plain JSON objects."
)


class ClaudeCodeOperationRunner:
    """Runs one workflow activity as an autonomous Claude Code CLI task."""

    def __init__(self, db: Session):
        self.db = db

    def provider_available(self) -> bool:
        """True when an enabled claude_code provider is registered (so the
        executor can fall back to the orchestrator engine with a clear error
        rather than crashing when the engine is requested but unconfigured)."""
        return self._resolve_provider() is not None

    def _resolve_provider(self):
        reg = ProviderRegistryService(self.db)
        from models.provider import Provider
        return (self.db.query(Provider)
                .filter(Provider.provider_type == "claude_code", Provider.enabled == True)  # noqa: E712
                .order_by(Provider.id.asc())
                .first())

    def _build_provider(self, operation_config: Dict[str, Any], model: Optional[str],
                        *, mcp_config_path: Optional[str] = None):
        p = self._resolve_provider()
        if p is None:
            raise RuntimeError(
                "The 'claude_code' execution engine was selected but no enabled "
                "Claude Code provider is registered. Add it under AI Models.")
        reg = ProviderRegistryService(self.db)
        key = reg.get_api_key(p.id)
        cfg: Dict[str, Any] = {}
        try:
            cfg = json.loads(p.config_json or "{}") or {}
        except Exception:  # noqa: BLE001
            pass

        exec_cfg = operation_config.get("execution") if isinstance(operation_config.get("execution"), dict) else {}
        # Model: per-step override wins, else the provider default, else a alias.
        # Coerce to a Claude model — a non-Claude id (e.g. an orchestrator pin)
        # can't run in the CLI.
        from services.providers.claude_code_provider import claude_code_model
        model_id = claude_code_model(model or exec_cfg.get("model") or cfg.get("default_model"))

        # Give the autonomous run ND3X's own tools + MCP servers (Fabric, board,
        # …) via the gateway, unless the step opts out. Web tools stay the CLI's
        # own (the gateway excludes them). This is the global default the user
        # chose; set execution.nd3x_tools=false to run fully isolated.
        extra_args = list(exec_cfg.get("extra_args") or [])
        if mcp_config_path:
            extra_args += ["--mcp-config", mcp_config_path]

        from services.providers.claude_code_provider import ClaudeCodeChatProvider
        return ClaudeCodeChatProvider(
            default_model=model_id,
            oauth_token=key,
            cli_path=str(cfg.get("cli_path") or "claude"),
            agentic=True,  # a workflow step is a full autonomous run
            allowed_tools=exec_cfg.get("allowed_tools") or cfg.get("allowed_tools"),
            max_turns=exec_cfg.get("max_turns") or cfg.get("max_turns") or 30,
            timeout=exec_cfg.get("timeout") or cfg.get("timeout"),
            workdir=exec_cfg.get("workdir") or cfg.get("workdir"),
            extra_args=extra_args,
        )

    @staticmethod
    def _want_nd3x_tools(operation_config: Dict[str, Any]) -> bool:
        exec_cfg = operation_config.get("execution") if isinstance(operation_config.get("execution"), dict) else {}
        return bool(exec_cfg.get("nd3x_tools", True))

    @staticmethod
    def _write_gateway_config() -> Optional[str]:
        """Write the ND3X MCP gateway --mcp-config to a temp file for the CLI."""
        try:
            import tempfile
            from services.mcp.mcp_gateway import mcp_config_for_cli
            cfg = mcp_config_for_cli()
            fd, path = tempfile.mkstemp(prefix="nd3x-mcp-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            return path
        except Exception as exc:  # noqa: BLE001 — the run can still proceed without ND3X tools
            log.warningx("ND3X MCP gateway config schrijven mislukt — stap draait zonder ND3X-tools",
                         error=str(exc))
            return None

    async def run(
        self,
        *,
        question: str,
        operation_config: Dict[str, Any],
        run_transcript: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
        operation_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        # Expose ND3X's tools + MCP servers to the autonomous run via the stdio
        # gateway (unless the step opted out). Written to a temp --mcp-config the
        # CLI loads; removed after the run.
        mcp_config_path: Optional[str] = None
        if self._want_nd3x_tools(operation_config or {}):
            mcp_config_path = self._write_gateway_config()

        provider = self._build_provider(operation_config or {}, model, mcp_config_path=mcp_config_path)
        prompt = self._build_prompt(question, run_transcript)
        trace: List[Dict[str, Any]] = [{
            "type": "claude_code_operation_start",
            "level": "info",
            "operation_id": operation_id,
            "workflow_run_id": workflow_run_id,
            "nd3x_tools": mcp_config_path is not None,
        }]
        log.infox(
            "Claude Code workflow operation run gestart",
            workflow_run_id=workflow_run_id, operation_id=operation_id,
            question_length=len(question or ""), nd3x_tools=mcp_config_path is not None,
        )
        try:
            result = await provider.chat(prompt, instructions=_handoff_instruction())
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except Exception:  # noqa: BLE001
                    pass
        answer, handoff = self._parse_envelope(result.text)
        trace.append({
            "type": "claude_code_operation_end",
            "level": "info",
            "operation_id": operation_id,
            "session_id": result.response_id or None,
            "handoff_status": (handoff or {}).get("status") if isinstance(handoff, dict) else None,
            "usage": result.usage or {},
        })
        log.infox(
            "Claude Code workflow operation run afgerond",
            workflow_run_id=workflow_run_id, operation_id=operation_id,
            handoff_status=(handoff or {}).get("status") if isinstance(handoff, dict) else None,
            answer_length=len(answer or ""),
        )
        # Shaped like a pipeline result so the executor's existing handoff gate,
        # For-Each iterables and follow-up routing all apply unchanged.
        return {
            "mode": "final",
            "answer": answer,
            "downstream_handoff": handoff,
            "tool_calls": None,
            "tool_results": None,
            "docs": None,
            "trace": trace,
            "terminal_state": "completed",
            "engine": "claude_code",
        }

    @staticmethod
    def _build_prompt(question: str, run_transcript: Optional[List[Dict[str, Any]]]) -> str:
        parts: List[str] = []
        if run_transcript:
            lines = []
            for m in run_transcript:
                role = (m.get("role") or "").strip() or "note"
                content = m.get("content")
                if isinstance(content, (dict, list)):
                    content = json.dumps(content, ensure_ascii=False)
                lines.append(f"- {role}: {str(content)[:1500]}")
            if lines:
                parts.append(
                    "Earlier steps of this workflow run and their outcomes "
                    "(build on them, do not redo):\n" + "\n".join(lines))
        parts.append(f"Task for this step:\n{question}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_envelope(text: str) -> tuple[str, Optional[Dict[str, Any]]]:
        """Extract the answer + downstream_handoff from the CLI's final message.

        The step is instructed to end with a single JSON object; be tolerant of
        prose around it (scan for the last balanced {...}). Falls back to using
        the whole text as the answer with a success handoff when no JSON is
        found — an autonomous run that produced prose still shouldn't hard-fail.
        """
        text = (text or "").strip()
        obj = ClaudeCodeOperationRunner._last_json_object(text)
        if isinstance(obj, dict) and ("answer" in obj or "downstream_handoff" in obj):
            answer = str(obj.get("answer") or "")
            handoff = obj.get("downstream_handoff")
            if handoff is not None and not isinstance(handoff, dict):
                handoff = None
            return answer, handoff
        return text, {
            "summary": text[:500],
            "full_answer": text or None,
            "artifacts": [], "facts": {}, "iterables": {}, "open_questions": [],
            "status": "success",
        }

    @staticmethod
    def _last_json_object(text: str) -> Any:
        # Try whole-string first, then the last {...} span (handles trailing/leading prose).
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
        depth = 0
        end = -1
        for i in range(len(text) - 1, -1, -1):
            c = text[i]
            if c == "}":
                if depth == 0:
                    end = i
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0 and end != -1:
                    try:
                        return json.loads(text[i:end + 1])
                    except Exception:  # noqa: BLE001
                        end = -1
        return None
