from __future__ import annotations

from typing import Any

from services.assistants.runtime.base import RuntimeAssistant


class RouterRuntimeAssistant(RuntimeAssistant):
    def prompt(self, question: str, **payload: Any) -> str:
        available_assistants = payload.pop("_available_assistants", [])
        available_workflows = payload.pop("_available_workflows", [])
        return self.prompt_builder.build_router_prompt(
            assistant=self.config,
            available_assistants=available_assistants,
            available_workflows=available_workflows,
            question=question,
            payload=payload,
        )