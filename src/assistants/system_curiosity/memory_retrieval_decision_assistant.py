from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class PlannerMemoryRetrievalDecisionAssistant(SystemAssistantBase):
    name = "planner_memory_retrieval_decision_assistant"

    @property
    def instructions(self) -> str:
        return (
            "You are an internal nano assistant. "
            "You never answer the user directly. "
            "Return exactly one JSON object. "
            "Allowed action is only finished. "
            "Your job is only to decide whether planner memory retrieval is useful, "
            "and if so, produce a compact retrieval query plus preferred scopes/types."
        )

    def prompt(
        self,
        *,
        question: str,
        active_conversation_state: Dict[str, Any] | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You decide whether long-term planner memory retrieval is useful before routing/planning.

You are NOT the router.
You are NOT the planner.
You do NOT answer the user.
You do NOT retrieve memories yourself.
You only decide whether retrieval is useful and provide retrieval direction.

Return exactly one JSON object:

{{
  "action": "finished",
  "should_retrieve": true,
  "reason": "string",
  "query": "string|null",
  "scopes": ["thread", "project", "global"],
  "types": [
    "user_preference",
    "project_memory",
    "architecture_decision",
    "implementation_detail",
    "correction"
  ]
}}

Meaning:
- should_retrieve: true only when durable memory may materially improve the next router/planner step.
- query: compact search query for memory retrieval, not a user-facing answer.
- scopes: preferred memory scopes. Backend will validate these.
- types: preferred memory types. Backend will validate these.

Use should_retrieve=false when:
- the user is clearly responding to the previous assistant message
- active_conversation_state is enough
- the message is a short confirmation, cancellation, frustration, or correction that can be resolved from active state
- retrieval would likely add noise
- the request is self-contained and obvious

Examples where should_retrieve=false:
- "yes"
- "doe maar"
- "nee"
- "stop met vragen"
- "Jezus. Herschrijf het document man! Wat een vragen...."
- "die reis was op 11 mei"
- "klopt"
- "ga door"

Use should_retrieve=true when:
- durable user preferences may affect execution
- project memories may affect implementation
- architecture decisions or implementation details may matter
- the user asks about earlier agreements, defaults, project setup, workflow behavior, assistant behavior, or recurring processes
- the current task is new and memory could improve correctness

Scope guidance:
- Use thread when there is an active conversation and the user refers to prior messages or ongoing work.
- Use project only when project_id is present and project context may matter.
- Use global for durable user preferences and cross-thread defaults.
- If this is a first/self-contained task with no active conversation, thread is usually unnecessary.
- If project_id is missing, do not include project.

Type guidance:
- user_preference: style, workflow, defaults, recurring user choices.
- project_memory: project/system facts.
- architecture_decision: design choices, routing/orchestrator/workflow architecture.
- implementation_detail: code, endpoints, services, models, repositories, tool behavior.
- correction: user corrections, "no not that", "I said always use X", "don't ask Y".

Do not include:
- router_memory
- semantic_memory
- note
- beliefs

Question:
{question}

Thread id:
{thread_id}

Project id:
{project_id}

Active conversation state:
{json.dumps(active_conversation_state or {}, ensure_ascii=False)[:9000]}
""".strip()


class RouterMemoryRetrievalDecisionAssistant(SystemAssistantBase):
    name = "router_memory_retrieval_decision_assistant"

    @property
    def instructions(self) -> str:
        return (
            "You are an internal nano assistant. "
            "You never answer the user directly. "
            "Return exactly one JSON object. "
            "Allowed action is only finished. "
            "Your job is only to decide whether router memory retrieval is useful."
        )

    def prompt(
        self,
        *,
        question: str,
        active_conversation_state: Dict[str, Any] | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You decide whether router memory retrieval is useful before routing.

Router memories only help select the right assistant, workflow, or skill.
They are NOT domain knowledge.
They are NOT task content.

Return exactly one JSON object:

{{
  "action": "finished",
  "should_retrieve": true,
  "reason": "string",
  "query": "string|null"
}}

Use should_retrieve=false when:
- the current request clearly maps to one assistant/workflow
- active_conversation_state clearly indicates continuation with the previous assistant
- the user is just confirming, cancelling, or answering a direct question
- routing is obvious
- retrieving router memories would add noise

Use should_retrieve=true when:
- the request is ambiguous
- the request could map to several assistants/workflows
- the user says broad things like "fix this", "check this", "build this", "make this work"
- assistant/workflow selection may benefit from prior routing preferences

If retrieval is useful, query should describe the routing problem, not the full task content.

Good router query examples:
- "frontend Lovable UI project thread management"
- "orchestrator cognition memory routing debugging"
- "document update declaration assistant selection"
- "workflow operation backend implementation"

Question:
{question}

Thread id:
{thread_id}

Project id:
{project_id}

Active conversation state:
{json.dumps(active_conversation_state or {}, ensure_ascii=False)[:7000]}
""".strip()