from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class CognitionRouterSystemAssistant(SystemAssistantBase):
    name = "cognition_router_system_assistant"

    def prompt(
        self,
        *,
        question: str,
        answer: str,
        existing_context: Dict[str, Any],
        project_id: str | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You are the routing layer for internal system cognition.

Your job is to decide how much cognition this completed user turn deserves.

Do NOT answer the user.
Do NOT store anything.
Do NOT research anything.

Choose one mode:

skip:
- pure small talk
- short confirmations such as "yes", "ok", "doe maar", "ga door", "klopt"
- cancellation or acknowledgement turns
- successful execution acknowledgements
- no reusable information
- no useful future learning
- routine document updates or CRUD actions with no durable user/project information
- temporary execution state only, such as doc_id, file_path, tool result, confirmation state, or generated content

light:
- minor durable preference
- small correction
- small implementation detail
- useful memory, but no research needed
- reusable project detail that does not justify curiosity or research

standard:
- technical/project/design/domain discussion
- durable implementation or architecture information
- useful for memory and maybe curiosity
- may create research jobs, but should not run deep research immediately

deep:
- high-value architecture decision
- important domain comparison
- recurring agent/worldview topic
- major design tradeoff
- topic likely worth immediate research and belief synthesis

Return exactly one JSON object:

{{
  "action": "finished",
  "mode": "skip | light | standard | deep",
  "reason": "...",
  "run_interpretation": true,
  "run_memory": true,
  "run_curiosity": true,
  "run_deep_research": false,
  "priority": 0.0
}}

Hard consistency rules:
- If mode is "skip", you MUST set:
  - run_interpretation=false
  - run_memory=false
  - run_curiosity=false
  - run_deep_research=false
  - priority=0.0
- If mode is "light", usually set:
  - run_interpretation=true
  - run_memory=true
  - run_curiosity=false
  - run_deep_research=false
- If mode is "standard", usually set:
  - run_interpretation=true
  - run_memory=true
  - run_curiosity=true
  - run_deep_research=false
- If mode is "deep", usually set:
  - run_interpretation=true
  - run_memory=true
  - run_curiosity=true
  - run_deep_research=true

Rules:
- Prefer skip for confirmation-only turns.
- Prefer skip for tool execution results that only confirm a mutation.
- Prefer skip for one-off document updates, expense edits, generated markdown, and persistence tasks unless they reveal durable project rules.
- Prefer light when memory extraction is useful but research would be overkill.
- Prefer standard for normal useful technical conversations.
- Use deep sparingly, only when immediate worldview formation is worth the cost.
- If the user is discussing assistant architecture, memory, beliefs, cognition, orchestrator, agents, or data/platform architecture, usually use standard or deep.
- If the answer compares major technologies, platforms, architectural choices, or tradeoffs, use standard or deep.

Never run curiosity or deep research for:
- simple CRUD actions
- document updates
- confirmation turns
- expense/declaration row updates
- formatting or persistence tasks
- generated document previews
unless the user explicitly asks for research or the turn reveals a durable design decision.

Question:
{question}

Answer:
{answer}

Project id:
{project_id}

Existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:6000]}

Previous tool results:
{json.dumps(previous_tool_results or {}, ensure_ascii=False)[:6000]}
""".strip()