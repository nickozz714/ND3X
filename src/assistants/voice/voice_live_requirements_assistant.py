# assistants/voice_live_requirements_assistant.py
from __future__ import annotations
import json
from typing import Any
from assistants.voice.assistant_base import BaseAssistant

class VoiceLiveRequirementsAssistant(BaseAssistant):
    """
    Incremental Requirements Engineering live updater.
    Maintains a structured requirements state (not generic meeting notes).
    """
    name = "VoiceLiveRequirementsAssistant"

    instructions: str = (
        "You are a live Requirements Engineering assistant.\n"
        "You receive an existing JSON state and a NEW transcript delta.\n"
        "Update the state incrementally.\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON.\n"
        "- Do NOT wrap in markdown fences.\n"
        "- Do NOT include commentary outside JSON.\n"
        "- Do NOT invent facts not supported by (a) current_state or (b) transcript delta.\n\n"
        "CRITICAL STATE PRESERVATION RULES:\n"
        "- Treat current_state as the accumulated RE document so far.\n"
        "- Do NOT delete prior items unless explicitly corrected.\n"
        "- Avoid duplicates: merge/update by meaning.\n\n"
        "GOALS:\n"
        "- Keep a short project context and scope.\n"
        "- Capture stakeholders.\n"
        "- Capture glossary/definitions when terms appear.\n"
        "- Extract functional requirements and user stories.\n"
        "- Extract non-functional requirements (performance/security/privacy/etc.).\n"
        "- Track acceptance criteria when mentioned.\n"
        "- Track assumptions, constraints, risks, decisions, open questions.\n"
    )

    def schema(self) -> str:
        schema_obj = {
            "type": "json_schema",
            "json_schema": {
                "name": "requirements_live_state",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "context": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "goal": {"type": "string"},
                                "scope_in": {"type": "array", "items": {"type": "string"}},
                                "scope_out": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title", "goal", "scope_in", "scope_out"],
                        },
                        "stakeholders": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "role": {"type": "string"},
                                    "needs": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["name", "role", "needs"],
                            },
                        },
                        "glossary": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "term": {"type": "string"},
                                    "definition": {"type": "string"},
                                },
                                "required": ["term", "definition"],
                            },
                        },
                        "user_stories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string"},
                                    "story": {"type": "string"},  # "As a ... I want ... so that ..."
                                    "priority": {"type": "string", "enum": ["must", "should", "could", "wont", "unknown"]},
                                    "status": {"type": "string", "enum": ["new", "refining", "approved", "done"]},
                                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                                    "evidence": {"type": "string"},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                },
                                "required": ["id", "story", "priority", "status", "acceptance_criteria", "evidence", "start_s", "end_s"],
                            },
                        },
                        "functional_requirements": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string"},
                                    "requirement": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "priority": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                                    "status": {"type": "string", "enum": ["new", "refining", "approved", "implemented"]},
                                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                                    "dependencies": {"type": "array", "items": {"type": "string"}},
                                    "evidence": {"type": "string"},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                },
                                "required": ["id", "requirement", "rationale", "priority", "status", "acceptance_criteria", "dependencies", "evidence", "start_s", "end_s"],
                            },
                        },
                        "nonfunctional_requirements": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string"},
                                    "category": {"type": "string"},  # performance/security/privacy/usability/...
                                    "requirement": {"type": "string"},
                                    "metric": {"type": "string"},
                                    "priority": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                                    "status": {"type": "string", "enum": ["new", "refining", "approved", "implemented"]},
                                    "evidence": {"type": "string"},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                },
                                "required": ["id", "category", "requirement", "metric", "priority", "status", "evidence", "start_s", "end_s"],
                            },
                        },
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "risks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "risk": {"type": "string"},
                                    "impact": {"type": "string"},
                                    "mitigation": {"type": "string"},
                                },
                                "required": ["risk", "impact", "mitigation"],
                            },
                        },
                        "decisions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "decision": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "evidence": {"type": "string"},
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                },
                                "required": ["decision", "rationale", "evidence", "start_s", "end_s"],
                            },
                        },
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "context",
                        "stakeholders",
                        "glossary",
                        "user_stories",
                        "functional_requirements",
                        "nonfunctional_requirements",
                        "assumptions",
                        "constraints",
                        "risks",
                        "decisions",
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
        lang = payload.get("lang") or "auto"

        ctx = {
            "lang": lang,
            "delta_time_range": delta_time_range,
            "merge_rules": [
                "Preserve everything already in current_state unless explicitly corrected in delta.",
                "Append new items from delta.",
                "Deduplicate by meaning (merge similar requirements/stories).",
                "If a requirement is refined, update the existing entry rather than creating a new one.",
                "Assign IDs if missing: US-###, FR-###, NFR-### (keep stable once created).",
                "Evidence should be a short quote or paraphrase from the delta.",
            ],
        }

        return (
            "Update the accumulated Requirements Engineering state.\n"
            "You MUST preserve current_state and merge/append using ONLY the new delta.\n"
            "Return JSON only.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"Current state JSON:\n{json.dumps(current_state, ensure_ascii=False)}\n\n"
            f"New transcript delta:\n{delta_transcript}\n\n"
            f"Return schema:\n{self.schema()}"
        )
