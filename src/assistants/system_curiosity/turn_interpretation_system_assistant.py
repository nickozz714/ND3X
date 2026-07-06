from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class TurnInterpretationSystemAssistant(SystemAssistantBase):
    name = "turn_interpretation_system_assistant"

    def prompt(
        self,
        *,
        question: str,
        answer: str,
        existing_context: Dict[str, Any],
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You are the first cognition layer after a completed user turn.

Your job is to decompose the raw turn into structured cognitive material for downstream:
- memory extraction
- curiosity planning
- autonomous research
- belief/worldview formation

Do NOT answer the user.
Do NOT store anything.
Do NOT research anything.

Be precise and selective.
Extract only cognitive material that is actually reusable.

Return exactly one JSON object:

{{
  "action": "finished",
  "interpretation": {{
    "turn_summary": "...",
    "main_topics": ["..."],
    "technical_concepts": ["..."],
    "project_context": ["..."],
    "architecture_decisions": ["..."],
    "implementation_details": ["..."],
    "user_preferences": ["..."],
    "constraints": ["..."],
    "corrections": ["..."],
    "tradeoffs": ["..."],
    "open_questions": ["..."],
    "agent_behavior_implications": ["..."],
    "researchworthy_topics": [
      {{
        "topic": "...",
        "reason": "...",
        "suggested_depth": "small | medium | deep",
        "priority": 0.0
      }}
    ],
    "candidate_memories": [
      {{
        "type": "project_memory | user_preference | architecture_decision | implementation_detail | correction | semantic_memory | note",
        "content": "...",
        "importance": 0.0,
        "reason": "..."
      }}
    ],
    "worldview_seeds": [
      {{
        "topic": "...",
        "claim": "...",
        "why_it_matters": "...",
        "possible_tradeoff": "...",
        "confidence_hint": 0.0
      }}
    ],
    "candidate_belief_seeds": [
      {{
        "topic": "...",
        "summary": "...",
        "why_it_matters": "...",
        "confidence_hint": 0.0
      }}
    ]
  }}
}}

Extraction rules:
- For rich technical turns, extract decisions, constraints, tradeoffs, corrections, and implementation implications.
- For simple operational turns, keep lists mostly empty.
- Extract worldview_seeds only when the turn implies a reusable principle beyond the current task.
- Extract agent_behavior_implications only when the user reveals how the assistant/system should behave in the future.
- Extract researchworthy_topics only when learning more could materially improve future reasoning.
- Do not invent personal facts.
- Do not infer sensitive traits.
- Do not collapse everything into one generic summary.
- Prefer concrete cognitive material over vague statements.

Do NOT create researchworthy_topics for routine operational turns, including:
- document updates
- text_update/text_ingest operations
- confirmations
- expense/declaration edits
- formatting changes
- successful tool execution summaries
- generated markdown or rewritten document content

Only create researchworthy_topics when:
- the user asks an open-ended research question
- there is a clear unknown that impacts future architecture
- the answer would benefit from external information
- the topic is reusable beyond the current turn
- the topic is not merely a generic abstraction of a simple action

Do NOT create candidate memories for:
- temporary execution state
- doc_id values
- file paths from one run
- pending confirmation state
- generated document content
- calculated totals from one document update
- routine mutation confirmations
- system behavior that is already hardcoded, such as requiring confirmation before mutations

Question:
{question}

Answer:
{answer}

Existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:12000]}

Previous tool results:
{json.dumps(previous_tool_results or {}, ensure_ascii=False)[:10000]}
""".strip()