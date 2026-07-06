# assistants/voice_live_requirements_finalize_assistant.py
from __future__ import annotations

import json
from typing import Any, Dict

from assistants.voice.assistant_base import BaseAssistant


class VoiceLiveRequirementsFinalizeAssistant(BaseAssistant):
    """
    Finalize/polish the accumulated Requirements Engineering LIVE state into a clean RE document.

    Input: state (accumulated JSON, produced by VoiceLiveRequirementsAssistant)
    Output: JSON only: { "title": "...", "text": "..." }
    """

    name = "VoiceLiveRequirementsFinalizeAssistant"

    instructions: str = (
        "You are a Requirements Engineering document finalization assistant.\n"
        "You receive an accumulated Requirements Engineering state JSON.\n"
        "Produce a clean, well-structured final Requirements document in Markdown.\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON.\n"
        "- Do NOT wrap output in markdown fences.\n"
        "- Do NOT include any commentary outside JSON.\n"
        "- Return exactly one JSON object with keys: title, text.\n\n"
        "FACTUALITY RULES:\n"
        "- Do NOT invent facts or add requirements not present in the state.\n"
        "- If something is unclear, keep it as an open question instead of guessing.\n\n"
        "FORMATTING RULES:\n"
        "- Produce Markdown with clear headings and lists.\n"
        "- Prefer concise, unambiguous requirements statements.\n"
        "- Keep duplicates merged; prefer the most refined wording.\n"
        "- Keep IDs stable and visible (US-###, FR-###, NFR-###).\n"
        "- Render acceptance criteria as checklists.\n"
        "- Where evidence exists, include a short quote (1 line max).\n\n"
        "DOCUMENT STRUCTURE (use these sections, omit empty ones):\n"
        "1) Title (H1)\n"
        "2) Goal\n"
        "3) Scope (In scope / Out of scope)\n"
        "4) Stakeholders\n"
        "5) Glossary\n"
        "6) User Stories\n"
        "7) Functional Requirements\n"
        "8) Non-Functional Requirements\n"
        "9) Assumptions\n"
        "10) Constraints\n"
        "11) Risks\n"
        "12) Decisions\n"
        "13) Open Questions\n"
        "14) Notes\n"
    )

    def schema(self) -> str:
        schema_obj = {
            "type": "json_schema",
            "json_schema": {
                "name": "requirements_engineering_finalize",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["title", "text"],
                },
            },
        }
        return json.dumps(schema_obj, ensure_ascii=False)

    @staticmethod
    def _norm_list(xs: Any) -> list[str]:
        if not isinstance(xs, list):
            return []
        out: list[str] = []
        seen = set()
        for x in xs:
            if not isinstance(x, str):
                continue
            s = x.strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out

    def prompt(self, _: str = "", **payload: Any) -> str:
        state: Dict[str, Any] = payload.get("state") or {}
        lang = payload.get("lang") or "auto"

        # Optional knobs (safe defaults)
        include_evidence = bool(payload.get("include_evidence", True))
        max_quote_len = int(payload.get("max_quote_len") or 160)

        ctx = {
            "lang": lang,
            "include_evidence": include_evidence,
            "max_quote_len": max_quote_len,
            "quality_rules": [
                "Do not invent anything not present in the state.",
                "If a requirement is duplicated, keep only the best version.",
                "Acceptance criteria should be short and testable.",
                "If scope is missing, leave scope sections out rather than guessing.",
                "If the title is not present, infer a generic title from context (e.g., 'Requirements Session').",
            ],
            "rendering_rules": [
                "Use headings with emojis as in the structure rules.",
                "User Stories should show: ID, priority, status, story.",
                "Functional and Non-Functional requirements should show: ID, priority, status, requirement, rationale/metric.",
                "Acceptance criteria should be checkboxes: - [ ] ...",
                "Evidence (if present) should be a single short quote line.",
            ],
        }

        return (
            "Finalize the Requirements Engineering document.\n"
            "Return ONLY JSON with keys: title, text.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"State JSON:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"Return schema:\n{self.schema()}"
        )
