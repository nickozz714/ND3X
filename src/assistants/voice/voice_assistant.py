# assistants/voice_assistant.py
from __future__ import annotations

import json
from typing import Any, Dict

from assistants.voice.assistant_base import BaseAssistant


class VoiceAssistant(BaseAssistant):
    """
    Produces a "meeting intelligence" JSON object from a transcript.

    Covers requested features:
      - Speaker labeling (via speaker IDs in provided diarized segments)
      - AI summaries + multidimensional summaries (views)
      - Key highlights (time-coded)
      - Action items (time-coded, owner attributed via speaker_id/name)
      - Decision logs (structured, time-coded)
      - Mind maps (Mermaid mindmap or tree)
    """

    name = "VoiceAssistant"

    # Keep it strict: JSON only
    instructions: str = (
        "You are a meeting/voice transcript assistant.\n"
        "You will be given a transcript and may also receive diarized segments and a speaker list.\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON that matches the provided JSON Schema.\n"
        "- Do NOT wrap in markdown fences.\n"
        "- Do NOT include any commentary outside JSON.\n"
        "- Do NOT invent facts, names, owners, dates, or decisions.\n"
        "- If something is uncertain, keep it null/empty and add an item in questions.\n\n"
        "EVIDENCE & TIMESTAMPS:\n"
        "- Prefer using diarized_segments for evidence.\n"
        "- For highlights, action_items, and decision_log: include start_s/end_s when available.\n"
        "- Evidence must be a short exact quote from the transcript (verbatim snippet), not a paraphrase.\n"
        "- If timestamps are not available, use null for start_s/end_s and still include evidence.\n\n"
        "TASKS:\n"
        "1) Summary: 2–6 sentences.\n"
        "2) Views (multidimensional summaries):\n"
        "   - exec: 3–7 bullets worth of text (concise, outcomes + next steps).\n"
        "   - detailed: a structured paragraph-style summary.\n"
        "   - bullets: an array of 5–12 key bullets.\n"
        "3) Speakers:\n"
        "   - If speakers are provided, keep them and DO NOT invent names.\n"
        "   - If you can confidently map a speaker_id to a real name mentioned (\"I'm Alex\"), set that speaker.name.\n"
        "4) Highlights:\n"
        "   - Provide 3–10 highlights max.\n"
        "   - Types: decision, action, risk, insight, question.\n"
        "   - Each highlight MUST have evidence and preferably timestamps.\n"
        "5) Action items:\n"
        "   - Extract true tasks (not general notes).\n"
        "   - Owner: prefer owner_speaker_id if clear from who said it.\n"
        "   - If owner unclear, set owner_speaker_id and owner_name to null and add a question.\n"
        "6) Decision log:\n"
        "   - Only include explicit decisions.\n"
        "   - Include rationale if stated.\n"
        "7) Mind map:\n"
        "   - Return a mermaid mindmap in mind_map.content.\n"
        "   - Keep it readable: depth <= 4, total nodes <= 40.\n\n"
        "IMPORTANT:\n"
        "- Be conservative: if it's not clearly supported by the transcript, omit it.\n"
        "- Avoid duplicates; merge similar items.\n"
    )

    def schema(self) -> str:
        """
        Strict JSON schema for OpenAI Responses API response_format.
        """
        schema_obj: Dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": "voice_meeting_intelligence",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},

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

                        "speakers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string"},  # S1, S2, ...
                                    "name": {"type": ["string", "null"]},
                                },
                                "required": ["id", "name"],
                            },
                        },

                        "highlights": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["decision", "action", "risk", "insight", "question"],
                                    },
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

                                    "owner_speaker_id": {"type": ["string", "null"]},  # prefer S1/S2
                                    "owner_name": {"type": ["string", "null"]},        # optional name if explicit

                                    "due": {"type": ["string", "null"]},  # ISO-ish or natural language as spoken
                                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "status": {"type": "string", "enum": ["new", "in_progress", "blocked", "done"]},

                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                    "evidence": {"type": "string"},
                                },
                                "required": [
                                    "task",
                                    "owner_speaker_id",
                                    "owner_name",
                                    "due",
                                    "priority",
                                    "status",
                                    "start_s",
                                    "end_s",
                                    "evidence",
                                ],
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
                                    "owner": {"type": ["string", "null"]},  # approver/driver if explicit
                                    "start_s": {"type": ["number", "null"]},
                                    "end_s": {"type": ["number", "null"]},
                                    "evidence": {"type": "string"},
                                },
                                "required": ["decision", "rationale", "owner", "start_s", "end_s", "evidence"],
                            },
                        },

                        "mind_map": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "format": {"type": "string", "enum": ["mermaid"]},
                                "content": {"type": "string"},
                            },
                            "required": ["format", "content"],
                        },

                        "notes": {"type": "array", "items": {"type": "string"}},
                        "questions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "summary",
                        "views",
                        "speakers",
                        "highlights",
                        "action_items",
                        "decision_log",
                        "mind_map",
                        "notes",
                        "questions",
                    ],
                },
            },
        }
        return json.dumps(schema_obj, ensure_ascii=False)

    def prompt(self, question: str, **payload: Any) -> str:
        """
        question: transcript text (fallback)
        payload may include:
          - transcript: str
          - speakers: [{id,name}] from diarization pipeline
          - diarized_segments: [{start_s,end_s,speaker,text}] for evidence/timestamps
          - lang: "nl"/"en"/...
          - known_people: optional list of known names
        """
        transcript = payload.get("transcript") or question or ""

        lang = payload.get("lang")
        known_people = payload.get("known_people")

        # New: diarization context (preferred)
        speakers = payload.get("speakers") or []
        diarized_segments = payload.get("diarized_segments") or []

        # Keep prompt compact and deterministic.
        # IMPORTANT: we do NOT need to paste schema in the prompt if response_format is enforced,
        # but we can include it as a last-resort guardrail.
        ctx: Dict[str, Any] = {
            "lang": lang,
            "known_people": known_people,
            "speakers": speakers,
        }

        # Prefer diarized segments if present; otherwise fall back to transcript.
        # Providing diarized segments allows the model to select evidence with start/end timestamps.
        if diarized_segments:
            transcript_block = json.dumps(diarized_segments, ensure_ascii=False)
            transcript_label = "Diarized segments (preferred source of truth)"
        else:
            transcript_block = transcript
            transcript_label = "Transcript"

        return (
            "Extract meeting intelligence from the provided conversation.\n"
            "Return JSON only.\n"
            "Use timestamps (start_s/end_s) when available.\n"
            "Use short verbatim evidence quotes from the transcript.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"{transcript_label}:\n{transcript_block}\n\n"
            "JSON Schema (for reference):\n"
            f"{self.schema()}"
        )
