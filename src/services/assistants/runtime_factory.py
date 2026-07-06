from __future__ import annotations

from component.logging import get_logger
from services.assistants.runtime.final_answer import FinalAnswerRuntimeAssistant
from services.assistants.runtime.planner import PlannerRuntimeAssistant
from services.assistants.runtime.router import RouterRuntimeAssistant
from services.assistants.prompt_builder import PromptBuilder
from services.assistants.runtime_config import AssistantConfig


log = get_logger(__name__)


class AssistantRuntimeFactory:
    def __init__(self, prompt_builder: PromptBuilder | None = None):
        log.debugx(
            "AssistantRuntimeFactory initialiseren",
            has_prompt_builder=prompt_builder is not None,
        )
        self.prompt_builder = prompt_builder or PromptBuilder()
        log.debugx(
            "AssistantRuntimeFactory geïnitialiseerd",
            prompt_builder_type=type(self.prompt_builder).__name__,
        )

    def create(self, config: AssistantConfig):
        log.infox(
            "Runtime assistant aanmaken gestart",
            assistant_id=config.id,
            assistant_name=config.name,
            assistant_type=config.assistant_type,
        )

        if config.assistant_type == "router":
            log.infox(
                "RouterRuntimeAssistant wordt aangemaakt",
                assistant_id=config.id,
                assistant_name=config.name,
            )
            return RouterRuntimeAssistant(config=config, prompt_builder=self.prompt_builder)

        if config.assistant_type == "planner":
            log.infox(
                "PlannerRuntimeAssistant wordt aangemaakt",
                assistant_id=config.id,
                assistant_name=config.name,
            )
            return PlannerRuntimeAssistant(config=config, prompt_builder=self.prompt_builder)

        if config.assistant_type == "final_answer":
            log.infox(
                "FinalAnswerRuntimeAssistant wordt aangemaakt",
                assistant_id=config.id,
                assistant_name=config.name,
            )
            return FinalAnswerRuntimeAssistant(config=config, prompt_builder=self.prompt_builder)

        log.errorx(
            "Runtime assistant aanmaken mislukt: unsupported assistant_type",
            assistant_id=config.id,
            assistant_name=config.name,
            assistant_type=config.assistant_type,
        )
        raise ValueError(f"Unsupported assistant_type: {config.assistant_type}")