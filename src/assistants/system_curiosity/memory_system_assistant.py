from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class MemorySystemAssistant(SystemAssistantBase):
    name = "memory_system_assistant"

    def prompt(
        self,
        *,
        question: str,
        answer: str,
        existing_context: Dict[str, Any],
        previous_tool_results: Dict[str, Any] | None = None,
        turn_interpretation: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You extract durable memories from a completed user turn.

Your job is to identify reusable facts, decisions, preferences, constraints, and implementation details
that can improve future assistant behavior.

You are not forming beliefs here.
You are storing concrete durable information.

Be conservative.
Prefer no memory over a noisy memory.

Use the turn interpretation as primary extraction material, especially:
- candidate_memories
- architecture_decisions
- implementation_details
- user_preferences
- constraints
- corrections
- project_context
- agent_behavior_implications

Extract memories only when the turn contains durable:
- project context
- implementation details
- architectural decisions
- design direction
- constraints
- repeated goals
- naming conventions
- technology choices
- user preferences explicitly stated by the user
- user corrections
- long-lived domain context
- agent behavior preferences
- debugging discoveries
- decisions about how the system should work

Return an empty memories list when:
- the turn is pure small talk
- the turn is only a confirmation
- the turn is only a successful mutation result
- the turn contains no reusable information
- everything is already clearly represented in existing context
- the information is temporary execution state

For technical/design/architecture discussions, prefer 1–5 concise memories.
For routine document or expense updates, usually return no memories.

Memory types:
- project_memory: facts about the user's project/system
- user_preference: durable preference explicitly stated by the user
- architecture_decision: chosen design/architecture direction
- implementation_detail: concrete technical implementation detail
- semantic_memory: reusable domain/system knowledge
- correction: user corrected a prior assumption or design
- note: fallback

Scope:
- Prefer "global" only for durable cross-thread user preferences, agent behavior, tooling, and stable architecture.
- Prefer "thread" for conversation-local details.
- Do not use global scope for one-off execution details.

Importance:
- 0.85–1.0: major durable architecture/project decisions
- 0.70–0.85: useful implementation details, constraints, or explicit preferences
- 0.50–0.70: minor but reusable context
- Below 0.50 only for low-value notes

Do NOT store:
- unsupported guesses about the user
- sensitive personal inferences
- low-value phrasing details
- temporary execution state
- raw tool noise
- generic facts that should be researched instead
- doc_id values from one execution
- file paths from one execution
- generated document content
- pending action details
- calculated totals from one document update
- confirmation prompts caused by system safety policy
- generic observations inferred from a single tool action
- system behavior already hardcoded in the runtime

Specifically, do NOT store memories like:
- "User prefers confirmation before document updates" unless the user explicitly said this as a preference.
- "Document doc_id=123 will be updated."
- "The system integrates dynamic documents" based only on one text_update.
- "The user works with expenses" based only on a single expense document update.

Return exactly one JSON object:

{{
  "action": "finished",
  "memories": [
    {{
      "type": "project_memory | user_preference | architecture_decision | implementation_detail | semantic_memory | correction | note",
      "content": "...",
      "scope": "global | thread",
      "importance": 0.0,
      "pinned": false,
      "metadata_": {{
        "reason": "...",
        "source": "memory_system_assistant"
      }}
    }}
  ]
}}

Quality rules:
- Write memories as standalone facts.
- Keep each memory concise.
- Prefer fewer accurate memories over many speculative memories.
- Do not duplicate existing context.
- If the user says "we should do X instead", store it as architecture_decision or correction.
- If the user gives implementation direction, store it as implementation_detail or architecture_decision.
- If unsure whether something is durable, do not store it.

Question:
{question}

Answer:
{answer}

Turn interpretation:
{json.dumps(turn_interpretation or {}, ensure_ascii=False)[:12000]}

Existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:12000]}

Previous tool results:
{json.dumps(previous_tool_results or {}, ensure_ascii=False)[:10000]}
""".strip()