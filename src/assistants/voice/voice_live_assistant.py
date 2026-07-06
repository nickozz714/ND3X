# assistants/voice_live_assistant.py
from __future__ import annotations

import json
from typing import Any

from assistants.voice.assistant_base import BaseAssistant


class VoiceLiveAssistant(BaseAssistant):
    """
    Incremental live notes updater (NO diarization).

    IMPORTANT BEHAVIOR CHANGE (minimal but crucial):
      - You MUST preserve prior state and append new info.
      - Do NOT rewrite summary/bullets only from the last delta.
    """

    name = "VoiceLiveAssistant"

    instructions: str = (
        "You are a live meeting notes assistant.\n"
        "You receive an existing JSON state and a NEW transcript delta.\n"
        "Update the state incrementally.\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON.\n"
        "- Do NOT wrap in markdown fences.\n"
        "- Do NOT include any commentary outside JSON.\n"
        "- Do NOT invent facts not supported by (a) current_state or (b) transcript delta.\n\n"
        "CRITICAL STATE PRESERVATION RULES:\n"
        "- Treat current_state as the accumulated notes so far.\n"
        "- Do NOT delete prior bullets/highlights/items just because they aren't mentioned in the new delta.\n"
        "- Only remove/replace an existing item if the delta explicitly corrects it.\n"
        "- Avoid duplicates: merge/update when an item already exists.\n\n"
        "GOALS:\n"
        "- Maintain an executive summary that reflects the whole conversation so far (2-5 sentences).\n"
        "- Maintain key points as an accumulated bullet list (keep older items).\n"
        "- Extract highlights with timestamps and short evidence quotes.\n"
        "- Extract action items (owner=null unless clearly stated).\n"
        "- Extract decisions only when explicit.\n"
        "- Track sentiment at a high level.\n"
        "- Suggest 3-10 supportive questions to ask next.\n"
        "- Track open questions and notes.\n"
    )

    def schema(self) -> str:
        schema_obj = {
            "type": "json_schema",
            "json_schema": {
                "name": "voice_live_state",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "views": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "exec": {"type": "string"},
                                "detailed": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["exec", "detailed", "bullets"],
                        },
                        "highlights": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "type": {"type": "string"},
                                    "title": {"type": "string"},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                    "evidence": {"type": "string"},
                                },
                                "required": ["type", "title", "start_s", "end_s", "evidence"],
                            },
                        },
                        "action_items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "task": {"type": "string"},
                                    "owner": {"type": ["string", "null"]},
                                    "due": {"type": ["string", "null"]},
                                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "status": {"type": "string", "enum": ["new", "in_progress", "blocked", "done"]},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                    "evidence": {"type": "string"},
                                },
                                "required": ["task", "owner", "due", "priority", "status", "start_s", "end_s", "evidence"],
                            },
                        },
                        "decision_log": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "decision": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "owner": {"type": ["string", "null"]},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                    "evidence": {"type": "string"},
                                },
                                "required": ["decision", "rationale", "owner", "start_s", "end_s", "evidence"],
                            },
                        },
                        "sentiment": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "overall": {"type": "string", "enum": ["positive", "neutral", "tense", "mixed", "unclear"]},
                                "signals": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                            },
                            "required": ["overall", "signals", "confidence"],
                        },
                        "supportive_questions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "question": {"type": "string"},
                                    "why": {"type": "string"},
                                    "when_to_ask": {"type": "string"},
                                },
                                "required": ["question", "why", "when_to_ask"],
                            },
                        },
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "views",
                        "highlights",
                        "action_items",
                        "decision_log",
                        "sentiment",
                        "supportive_questions",
                        "open_questions",
                        "notes",
                    ],
                },
            },
        }
        return json.dumps(schema_obj, ensure_ascii=False)

    def prompt(self, _: str = "", **payload: Any) -> str:
        current_state = payload.get("current_state") or {}
        delta_transcript = payload.get("delta_transcript") or ""
        delta_time_range = payload.get("delta_time_range") or {"start_s": None, "end_s": None}
        lang = payload.get("lang")

        ctx = {
            "lang": lang,
            "delta_time_range": delta_time_range,
            "merge_rules": [
                "Preserve everything already in current_state unless explicitly corrected in delta.",
                "Append new info from delta.",
                "Deduplicate by meaning (similar wording). Update existing items rather than creating duplicates.",
                "Do not invent details not present in current_state or delta.",
            ],
        }

        return (
            "Update the accumulated meeting state.\n"
            "You MUST preserve current_state and merge/append using ONLY the new delta.\n"
            "Return JSON only.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"Current state JSON (accumulated so far):\n{json.dumps(current_state, ensure_ascii=False)}\n\n"
            f"New transcript delta (append this information):\n{delta_transcript}\n\n"
            f"Return schema:\n{self.schema()}"
        )
