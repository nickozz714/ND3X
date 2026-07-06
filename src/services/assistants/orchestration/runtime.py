from __future__ import annotations

from typing import Optional

from component.logging import get_logger
from services.assistants.runtime_config_loader import AssistantRuntimeConfigLoader
from services.assistants.runtime_factory import AssistantRuntimeFactory


log = get_logger(__name__)


class RuntimeResolver:
    def __init__(self, assistant_service):
        log.infox(
            "RuntimeResolver initialiseren",
            has_assistant_service=assistant_service is not None,
            assistant_service_type=type(assistant_service).__name__,
        )
        self.runtime_loader = AssistantRuntimeConfigLoader(assistant_service)
        log.debugx(
            "AssistantRuntimeConfigLoader aangemaakt",
            loader_type=type(self.runtime_loader).__name__,
        )
        self.runtime_factory = AssistantRuntimeFactory()
        log.debugx(
            "AssistantRuntimeFactory aangemaakt",
            factory_type=type(self.runtime_factory).__name__,
        )
        log.infox("RuntimeResolver geïnitialiseerd")

    def get_runtime_assistant_by_id_or_name(
        self,
        assistant_id: Optional[int] | None,
        assistant_name: Optional[str] | None,
    ):
        log.infox(
            "Runtime assistant ophalen gestart",
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            has_assistant_id=bool(assistant_id),
            has_assistant_name=bool(assistant_name),
        )
        if assistant_id:
            log.debugx(
                "Runtime assistant config ophalen op id",
                assistant_id=assistant_id,
            )
            config = self.runtime_loader.get_by_id(assistant_id)
        elif assistant_name:
            log.debugx(
                "Runtime assistant config ophalen op naam",
                assistant_name=assistant_name,
            )
            config = self.runtime_loader.get_by_name(assistant_name)
        else:
            log.warningx(
                "Runtime assistant ophalen mislukt: geen id of naam",
                assistant_id=assistant_id,
                assistant_name=assistant_name,
            )
            raise ValueError("Unknown assistant or no id or name provided")

        log.debugx(
            "Runtime assistant config gevonden",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            tool_count=len(getattr(config, "tools", []) or []),
        )
        runtime_assistant = self.runtime_factory.create(config)
        log.infox(
            "Runtime assistant ophalen afgerond",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            runtime_type=type(runtime_assistant).__name__,
        )
        return runtime_assistant

    def get_single_agent_runtime_assistant(self, *, allowed_builtin_tools: list[str] | None = None):
        log.infox("Single-agent runtime assistant ophalen gestart")
        config = self.runtime_loader.get_single_agent()
        # Per-activity builtin-tool allowlist: a workflow operation can restrict
        # which always-on builtin tools it may use (skill tools are unaffected).
        # Filtering config.tools hides the rest from the planner manifest, so the
        # model never sees them and cannot get a valid tool_id to call them.
        if allowed_builtin_tools:
            allow = {str(t).strip() for t in allowed_builtin_tools if str(t).strip()}
            config.tools = [t for t in (config.tools or []) if getattr(t, "name", None) in allow]
        runtime_assistant = self.runtime_factory.create(config)
        log.infox(
            "Single-agent runtime assistant ophalen afgerond",
            config_name=getattr(config, "name", None),
            skill_count=len(getattr(config, "skills", []) or []),
            runtime_type=type(runtime_assistant).__name__,
        )
        return runtime_assistant

    def get_router_runtime_assistant(self):
        log.infox("Router runtime assistant ophalen gestart")
        config = self.runtime_loader.get_router()
        log.debugx(
            "Router config gevonden",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            priority=getattr(config, "priority", None),
        )
        runtime_assistant = self.runtime_factory.create(config)
        log.infox(
            "Router runtime assistant ophalen afgerond",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            runtime_type=type(runtime_assistant).__name__,
        )
        return runtime_assistant

    def get_final_answer_runtime_assistant(self):
        log.infox("Final answer runtime assistant ophalen gestart")
        config = self.runtime_loader.get_final_answer()
        log.debugx(
            "Final answer config gevonden",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            priority=getattr(config, "priority", None),
        )
        runtime_assistant = self.runtime_factory.create(config)
        log.infox(
            "Final answer runtime assistant ophalen afgerond",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            runtime_type=type(runtime_assistant).__name__,
        )
        return runtime_assistant