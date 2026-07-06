from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class ResearchSystemAssistant(SystemAssistantBase):
    name = "research_system_assistant"

    @property
    def instructions(self) -> str:
        return (
            "You are an internal autonomous research planning assistant. "
            "Return exactly one JSON object. "
            "Allowed actions are only evaluate_answer and finished. "
            "Use evaluate_answer when EXA research can improve the agent's future worldview. "
            "Use finished only when previous_tool_results already contain sufficient research "
            "or when the topic is purely internal and external research would not help."
        )

    def prompt(
        self,
        *,
        topic: str,
        reason: str,
        depth: str,
        existing_context: Dict[str, Any],
        project_id: str | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        max_results = 3
        if depth == "medium":
            max_results = 4
        elif depth == "deep":
            max_results = 5

        return f"""
Plan research for an internal curiosity job.

Your goal is to gather just enough external material to improve future beliefs.
Be compact. Prefer one strong EXA query.

Use evaluate_answer when:
- the topic is technical, architectural, product, data, tooling, or domain-related
- external validation can sharpen tradeoffs, terminology, implementation patterns, or best practices
- previous_tool_results do not already contain useful research

Use finished when:
- previous_tool_results already contain sufficient useful research
- the topic is purely internal to the user's local implementation
- existing context already fully answers the learning objective
- external research would likely add noise

Return JSON for research:
{{
  "action": "evaluate_answer",
  "tool_calls": [
    {{
      "tool": "exa_research",
      "args": {{
        "query": "...",
        "num_results": {max_results},
        "text_char_limit": 1800
      }}
    }}
  ],
  "notes": "why this research is needed"
}}

Return JSON if no research is needed:
{{
  "action": "finished",
  "research_not_needed": true,
  "reason": "..."
}}

Query rules:
- Make the query specific.
- Include the practical angle.
- Avoid vague queries.
- Prefer one strong query.

Topic:
{topic}

Reason:
{reason}

Depth:
{depth}

Project id:
{project_id}

Compact existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:5000]}

Previous tool results:
{json.dumps(previous_tool_results or {}, ensure_ascii=False)[:8000]}
""".strip()