from __future__ import annotations

from typing import Any, Dict, Optional

from component.config import settings
from component.logging import get_logger
from services.assistants.orchestration.runtime import RuntimeResolver


log = get_logger(__name__)


class AssistantOperationRunner:
    def __init__(
        self,
        *,
        assistant_service,
        pipeline_runner,
    ):
        log.infox(
            "AssistantOperationRunner initialiseren",
            has_assistant_service=assistant_service is not None,
            has_pipeline_runner=pipeline_runner is not None,
            pipeline_runner_type=type(pipeline_runner).__name__,
        )
        self.runtime = RuntimeResolver(assistant_service)
        self.pipeline_runner = pipeline_runner
        log.infox(
            "AssistantOperationRunner geïnitialiseerd",
            runtime_type=type(self.runtime).__name__,
            pipeline_runner_type=type(self.pipeline_runner).__name__,
        )

    def _get_workflow_assistant(self, assistant_id: int):
        log.infox(
            "Workflow assistant ophalen gestart",
            assistant_id=assistant_id,
        )
        loader = self.runtime.runtime_loader

        if hasattr(loader, "get_by_id_for_workflow"):
            log.debugx(
                "Workflow assistant config ophalen via get_by_id_for_workflow",
                assistant_id=assistant_id,
            )
            config = loader.get_by_id_for_workflow(assistant_id)
        else:
            log.debugx(
                "Workflow assistant config ophalen via get_by_id fallback",
                assistant_id=assistant_id,
            )
            config = loader.get_by_id(assistant_id)

        log.debugx(
            "Workflow assistant config gevonden",
            assistant_id=assistant_id,
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            is_active=getattr(config, "is_active", None),
            is_router_selectable=getattr(config, "is_router_selectable", None),
            tool_count=len(getattr(config, "tools", []) or []),
        )

        assistant = self.runtime.runtime_factory.create(config)
        log.infox(
            "Workflow assistant ophalen afgerond",
            assistant_id=assistant_id,
            config_name=getattr(config, "name", None),
            runtime_assistant_type=type(assistant).__name__,
            runtime_assistant_name=getattr(assistant, "name", None),
        )
        return assistant

    async def run(
        self,
        *,
        assistant_id: int,
        question: str,
        payload: Dict[str, Any],
        workflow_run_id: int,
        operation_id: int,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        turn_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        log.infox(
            "AssistantOperationRunner run gestart",
            assistant_id=assistant_id,
            workflow_run_id=workflow_run_id,
            operation_id=operation_id,
            model=model,
            session_id=session_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            payload_keys=list((payload or {}).keys()),
        )

        # Single-agent model: a workflow assistant-activity is the ONE agent + the
        # operation's pre-given skill(s) (already set in payload._selected_skill_names by
        # the executor). No skill selection happens here — that's the cheap repeated-work
        # path. Legacy mode resolves the per-operation assistant by id.
        # Per-operation builtin-tool allowlist (config.builtin_tools): restrict the
        # always-on builtin tools this activity may use. Empty/absent = all (so a
        # step that just wants documents won't wander into e.g. the Fabric tool).
        _op_cfg = payload.get("operation_config") if isinstance(payload.get("operation_config"), dict) else {}
        _allowed_builtin = [str(x).strip() for x in (_op_cfg.get("builtin_tools") or []) if str(x).strip()]

        if getattr(settings, "SINGLE_AGENT_MODE", False):
            assistant = self.runtime.get_single_agent_runtime_assistant(
                allowed_builtin_tools=_allowed_builtin or None,
            )
            log.infox(
                "Workflow operation draait op de single agent (pre-given skills)",
                assistant_id=assistant_id,
                preselected_skills=(payload or {}).get("_selected_skill_names"),
                allowed_builtin_tools=_allowed_builtin or None,
            )
        else:
            assistant = self._get_workflow_assistant(assistant_id)
            if _allowed_builtin and getattr(assistant, "tools", None):
                allow = set(_allowed_builtin)
                assistant.tools = [t for t in assistant.tools if getattr(t, "name", None) in allow]

        run_payload = dict(payload or {})
        run_payload["_workflow_run_id"] = workflow_run_id
        run_payload["_workflow_operation_id"] = operation_id
        run_payload["_workflow_background"] = True
        run_payload["_cancellation_check"] = payload.get("_cancellation_check")
        # Workflows are fully autonomous: the agent never asks the user (ask_user is
        # converted to a terminal failure in the pipeline) — no opt-in.
        _opcfg = payload.get("operation_config") if isinstance(payload.get("operation_config"), dict) else {}
        # Per-operation light-mode override (compact planner prompt). "auto"/absent
        # keeps the per-model behaviour (auto = light for local models); "on"/"off"
        # force it for this step via _light_mode_session, which _resolve_light_mode
        # honours before the per-model toggle.
        _light = _opcfg.get("light_mode")
        if isinstance(_light, str):
            _light = _light.strip().lower()
            if _light in ("on", "light", "true"):
                run_payload["_light_mode_session"] = True
            elif _light in ("off", "full", "false"):
                run_payload["_light_mode_session"] = False
        elif isinstance(_light, bool):
            run_payload["_light_mode_session"] = _light
        # Per-operation manual overrides of the agent-loop budgets (e.g. a step that
        # legitimately runs long). max_wall_clock_seconds=0 → no time limit.
        _budget_overrides: Dict[str, Any] = {}
        for src_key, dst_key in (
            ("agent_max_wall_clock_seconds", "max_wall_clock_seconds"),
            ("agent_max_iterations", "max_iterations"),
            ("agent_max_tool_calls", "max_tool_calls"),
        ):
            v = _opcfg.get(src_key)
            if isinstance(v, int) and not isinstance(v, bool):
                _budget_overrides[dst_key] = v
        if _budget_overrides:
            run_payload["_agent_budget_overrides"] = _budget_overrides

        resolved_model = model  # None → orchestrator resolves the slot per stage
        assistant_name = getattr(assistant, "name", None) or f"assistant_{assistant_id}"

        resolved_session_id = (
                session_id
                or f"workflow:{workflow_run_id}:operation:{operation_id}:assistant:{assistant_name}"
        )
        resolved_turn_id = turn_id or operation_id

        log.infox(
            "Workflow assistant pipeline run voorbereiden",
            assistant_id=assistant_id,
            assistant_name=getattr(assistant, "name", None),
            workflow_run_id=workflow_run_id,
            operation_id=operation_id,
            resolved_model=resolved_model,
            resolved_session_id=resolved_session_id,
            resolved_turn_id=resolved_turn_id,
            run_payload_keys=list(run_payload.keys()),
            workflow_background=run_payload.get("_workflow_background"),
        )

        # A per-operation model override must WIN over the role's routing slot
        # (chat.planner), otherwise a workflow pinned to e.g. gpt-5.4-mini silently
        # ran on whatever local model the chat.planner slot points at (observed:
        # ~280s/hop on a local model instead of ~3s on the pinned cloud model). The
        # chat model picker already does this via forced_chat_model (precedence #0);
        # apply the same for the pinned operation model.
        from services.providers.chat_session import forced_chat_model
        _forced_token = forced_chat_model.set(resolved_model) if resolved_model else None
        try:
            result = await self.pipeline_runner.run(
                assistant=assistant,
                question=question,
                model=resolved_model,
                payload=run_payload,
                session_id=resolved_session_id,
                turn_id=resolved_turn_id,
                trace=[],
            )
        finally:
            if _forced_token is not None:
                forced_chat_model.reset(_forced_token)

        log.infox(
            "AssistantOperationRunner run afgerond",
            assistant_id=assistant_id,
            assistant_name=getattr(assistant, "name", None),
            workflow_run_id=workflow_run_id,
            operation_id=operation_id,
            result_mode=result.get("mode") if isinstance(result, dict) else None,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )
        return result