from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class ResearchObservationSystemAssistant(SystemAssistantBase):
    name = "research_observation_system_assistant"

    def prompt(
        self,
        *,
        topic: str,
        reason: str,
        depth: str,
        question: str,
        answer: str,
        research_docs: Dict[str, Any],
        existing_context: Dict[str, Any],
        turn_interpretation: Dict[str, Any] | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You distill research material into structured observations for later belief synthesis.

You are NOT creating beliefs yet.
You are NOT summarizing sources generically.
You are extracting analytical building blocks.

Your job is to identify:
- mechanisms
- tradeoffs
- recurring patterns
- implementation implications
- risks
- contradictions
- decision criteria
- useful heuristics
- source-backed claims
- gaps or uncertainty

These observations will be used by the BeliefSystemAssistant to create rich internal worldview capsules.

Return exactly one JSON object:

{{
  "action": "finished",
  "observation_pack": {{
    "topic": "...",
    "research_summary": "...",
    "core_observations": [
      {{
        "observation": "...",
        "why_it_matters": "...",
        "evidence": "...",
        "confidence": 0.0
      }}
    ],
    "mechanisms": [
      {{
        "mechanism": "...",
        "explanation": "...",
        "implication": "..."
      }}
    ],
    "tradeoffs": [
      {{
        "tradeoff": "...",
        "benefit": "...",
        "cost_or_risk": "...",
        "when_it_matters": "..."
      }}
    ],
    "implementation_implications": [
      {{
        "implication": "...",
        "applies_when": "...",
        "caution": "..."
      }}
    ],
    "decision_criteria": [
      {{
        "criterion": "...",
        "use_for": "...",
        "signal": "..."
      }}
    ],
    "contradictions_or_tensions": [
      {{
        "tension": "...",
        "explanation": "...",
        "resolution_hint": "..."
      }}
    ],
    "worldview_candidates": [
      {{
        "topic": "...",
        "claim": "...",
        "mechanism": "...",
        "tradeoff": "...",
        "future_use": ["..."],
        "confidence_hint": 0.0
      }}
    ],
    "evidence_refs": [
      {{
        "type": "exa | research_result | turn",
        "title": "...",
        "url": "...",
        "note": "..."
      }}
    ]
  }}
}}

Quality rules:
- Do not produce generic statements like "X improves performance".
- Always explain mechanism or tradeoff.
- Prefer 5–12 observations for rich topics.
- Extract what is useful for future reasoning, not just what the source says.
- If sources are weak or generic, say so in observations.
- Preserve uncertainty.
- Do not invent evidence.
- Use the original user turn and turn interpretation to focus the observations.

Topic:
{topic}

Reason:
{reason}

Depth:
{depth}

Original question:
{question}

Original answer:
{answer}

Turn interpretation:
{json.dumps(turn_interpretation or {}, ensure_ascii=False)[:12000]}

Research docs:
{json.dumps(research_docs or {}, ensure_ascii=False)[:22000]}

Existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:12000]}
""".strip()