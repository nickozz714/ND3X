from __future__ import annotations

from typing import Any

from services.assistants.runtime.base import RuntimeAssistant


class FinalAnswerRuntimeAssistant(RuntimeAssistant):
    def prompt(self, question: str, **payload: Any) -> str:
        return self.prompt_builder.build_final_answer_prompt(
            assistant=self.config,
            question=question,
            payload=payload,
        )