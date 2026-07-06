from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


class SystemAssistantBase:
    name = "system_assistant"

    @property
    def instructions(self) -> str:
        return (
            "You are an internal system assistant. "
            "You never answer the user directly. "
            "Return exactly one JSON object. "
            "Allowed actions are only evaluate_answer and finished. "
            "Use evaluate_answer when you need tools or another evaluation pass. "
            "Use finished when the internal task is complete."
        )

    def extract_first_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None

        text = text.strip()

        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None

        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None