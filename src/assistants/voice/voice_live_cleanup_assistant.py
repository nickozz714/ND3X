# assistants/voice_live_finalize_assistant.py
from __future__ import annotations

import json
from typing import Any, Dict

from assistants.voice.assistant_base import BaseAssistant


class VoiceLiveCleanupAssistant(BaseAssistant):
    """
    Final polish: turn accumulated JSON state into a clean Markdown note.
    Output is JSON with:
      - title: string
      - text: markdown string
    """

    name = "VoiceLiveCleanupAssistant"

    instructions: str = (
        "You are a note-polishing assistant.\n"
        "You receive an accumulated meeting state JSON.\n"
        "Produce a clean, well-structured Markdown document.\n\n"
        "OUTPUT FORMAT (STRICT):\n"
        "- Output ONLY a single valid JSON object.\n"
        "- No markdown outside the JSON.\n"
        "- JSON keys: title, text.\n"
        "- title: short, human-friendly meeting title.\n"
        "- text: the full Markdown notes.\n\n"
        "RULES:\n"
        "- Do NOT invent facts not present in the state.\n"
        "- Keep it readable, concise, and nicely formatted.\n"
        "- Use headings: Summary, Key points, Action items, Decisions, Highlights, Sentiment, Open questions, Notes.\n"
        "- Keep action items as Markdown checkboxes.\n"
        "- Keep evidence quotes short when present.\n"
    )

    def schema(self) -> dict:
        # JSON Schema (OpenAPI-achtig) — werkt ook prima als interne validatie
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1, "description": "Markdown notes"},
            },
            "required": ["title", "text"],
        }

    def prompt(self, _: str = "", **payload: Any) -> str:
        state: Dict[str, Any] = payload.get("state") or {}
        lang = payload.get("lang") or "auto"

        ctx = {
            "lang": lang,
            "style": [
                "Prefer a descriptive title (project/topic + date if present).",
                "If no clear topic exists, choose a generic but helpful title.",
                "Keep action items as checkboxes: - [ ] ...",
            ],
        }
        schema = self.schema()
        return (
            "Create FINAL polished meeting notes.\n"
            "Return ONLY one JSON object with keys: title, text.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"State JSON:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"Remember: output ONLY JSON, exactly like: \n {schema}\n"
        )
