from __future__ import annotations

import json
import re
from typing import Any

from component.logging import get_logger
from services.assistants.runtime_config import AssistantConfig
from services.assistants.prompt_builder import PromptBuilder


log = get_logger(__name__)


def _repair_json(text: str) -> Any:
    """Best-effort recovery of a JSON object/array from imperfect model output
    (code fences, trailing commas, smart quotes). Returns the parsed value or
    None if nothing usable can be recovered."""
    if not text:
        return None
    s = text
    # Drop ```json / ``` fences.
    s = re.sub(r"```[a-zA-Z0-9]*", "", s).replace("```", "")
    # Narrow to the outermost {...} or [...] span.
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    ends = [i for i in (s.rfind("}"), s.rfind("]")) if i != -1]
    if not starts or not ends:
        return None
    s = s[min(starts): max(ends) + 1]
    # Normalize smart quotes and strip trailing commas before } or ].
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    for candidate in (s, s.replace("'", '"')):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


class RuntimeAssistant:
    def __init__(self, config: AssistantConfig, prompt_builder: PromptBuilder):
        log.infox(
            "RuntimeAssistant initialiseren",
            config_id=getattr(config, "id", None),
            config_name=getattr(config, "name", None),
            assistant_type=getattr(config, "assistant_type", None),
            has_prompt_builder=prompt_builder is not None,
            prompt_builder_type=type(prompt_builder).__name__,
        )
        self.config = config
        self.prompt_builder = prompt_builder
        self.name = config.name
        self.instructions = config.instruction
        log.infox(
            "RuntimeAssistant geïnitialiseerd",
            name=self.name,
            instruction_length=len(self.instructions or ""),
            tool_count=len(getattr(config, "tools", []) or []),
        )

    def system(self):
        log.debugx(
            "RuntimeAssistant system messages bouwen",
            name=self.name,
            instruction_length=len(self.instructions or ""),
        )
        result = [{"role": "system", "content": self.instructions}]
        log.debugx(
            "RuntimeAssistant system messages gebouwd",
            name=self.name,
            message_count=len(result),
        )
        return result

    def extract_first_json_object(self, text: str) -> Any:
        log.infox(
            "Eerste JSON object uit model output extraheren gestart",
            assistant_name=self.name,
            text_length=len(text or ""),
        )

        # Local/open models often wrap JSON in <think>…</think> reasoning or
        # ```json fences. Strip reasoning blocks so a stray '{' inside them can't
        # derail the scan; fences are harmless (the scan skips to the first '{').
        if text and "<think>" in text:
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text or ""

        decoder = json.JSONDecoder()
        idx = 0
        length = len(text)

        while idx < length:
            ch = text[idx]
            if ch not in "{[":
                idx += 1
                continue
            try:
                log.debugx(
                    "JSON decode poging gestart",
                    assistant_name=self.name,
                    index=idx,
                    char=ch,
                )
                obj, end = decoder.raw_decode(text, idx)
                log.infox(
                    "Eerste JSON object uit model output geëxtraheerd",
                    assistant_name=self.name,
                    start_index=idx,
                    end_index=end,
                    object_type=type(obj).__name__,
                    object_keys=list(obj.keys()) if isinstance(obj, dict) else None,
                    item_count=len(obj) if isinstance(obj, list) else None,
                )
                return obj
            except json.JSONDecodeError:
                log.debugx(
                    "JSON decode poging mislukt",
                    assistant_name=self.name,
                    index=idx,
                )
                idx += 1

        # Repair pass for imperfect local-model JSON (trailing commas, code
        # fences, smart quotes) before giving up.
        repaired = _repair_json(text)
        if repaired is not None:
            log.infox("JSON via repair-pass geëxtraheerd", assistant_name=self.name)
            return repaired

        log.warningx(
            "Geen geldig JSON object of array gevonden in model output",
            assistant_name=self.name,
            text_length=len(text or ""),
        )
        raise ValueError("No valid JSON object or array found in model output.")