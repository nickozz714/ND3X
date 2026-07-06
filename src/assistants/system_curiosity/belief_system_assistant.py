from __future__ import annotations

import json
from typing import Any, Dict

from assistants.system_curiosity.base import SystemAssistantBase


class BeliefSystemAssistant(SystemAssistantBase):
    name = "belief_system_assistant"

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
        observation_pack: Dict[str, Any] | None = None,
        scope_context: Dict[str, Any] | None = None,
        previous_tool_results: Dict[str, Any] | None = None,
    ) -> str:
        return f"""
You synthesize internal worldview capsules for the agent.

A belief is NOT:
- a generic advice sentence
- a source summary
- a shallow best-practice note
- a one-line fact
- a generic abstraction from one routine tool action
- a memory of temporary execution state

A belief IS:
- a structured internal reasoning object
- a reusable interpretation
- a causal/tradeoff-aware heuristic
- a worldview capsule that can guide future answers

Use the observation_pack as your primary input.
Use research_docs only as supporting material.
Use the turn_interpretation to keep beliefs aligned with the user's actual context.

Return no beliefs when:
- the topic came from a routine document update
- the topic came from a confirmation turn
- the topic came from a simple CRUD operation
- the source material is too generic
- the belief would only restate common best practices
- the belief is not likely to influence future answers

Each belief must contain:
- a strong summary
- a content fallback
- 3–7 concrete insights
- future_use cases
- confidence
- status
- importance
- evidence_refs
- reasoning_summary

Beliefs must explain at least one of:
- mechanism: why this works
- tradeoff: when it helps vs hurts
- implication: how future reasoning should change
- decision criterion: when to choose one path over another
- risk: what to watch for

Prefer 1–3 deep beliefs for rich topics.
Prefer fewer deep beliefs over many shallow beliefs.
Do not create generic beliefs.

Bad belief:
"Indexing improves query performance."

Good belief:
"Indexing strategy in analytical models should be treated as a workload-specific tradeoff: narrow indexes can speed selective queries, but over-indexing increases write/maintenance cost and may underperform partitioning or clustering in lakehouse-style workloads."

Return exactly one JSON object:

{{
  "action": "finished",
  "beliefs": [
    {{
      "topic": "...",
      "summary": "...",
      "content": "...",
      "insights": [
        "...",
        "...",
        "..."
      ],
      "future_use": [
        "...",
        "..."
      ],
      "domain": "...",
      "confidence": 0.0,
      "status": "tentative | verified | disputed",
      "importance": 0.0,
      "scope": "global | project | thread",
      "use_when": ["..."],
      "evidence_refs": [
        {{
          "type": "exa | turn | memory | belief | research_result | observation",
          "title": "...",
          "url": "...",
          "note": "..."
        }}
      ],
      "contradictions": [],
      "metadata_": {{
        "source": "belief_system_assistant",
        "reasoning_summary": "..."
      }}
    }}
  ],
  "memories": [
    {{
      "type": "semantic_memory | architecture_decision | implementation_detail | project_memory",
      "content": "...",
      "scope": "global | project | thread",
      "importance": 0.0,
      "pinned": false,
      "metadata_": {{
        "source": "belief_system_assistant",
        "reason": "..."
      }}
    }}
  ]
}}

Strict quality rules:
- Never output a belief with only one insight unless the topic is extremely narrow.
- Never merely restate a source title or article summary.
- Every insight should be useful independently.
- At least one insight should mention a tradeoff, limitation, or condition.
- At least one insight should mention practical future application.
- Use confidence 0.40–0.65 for plausible but uncertain beliefs.
- Use confidence 0.65–0.85 for well-supported beliefs.
- Use confidence above 0.85 only with strong evidence.
- Prefer project scope when scope_context indicates the job belongs to a project.
- Prefer global scope only for durable agent architecture, tooling, data platforms, product categorization, and domain understanding that is not project-specific.
- Memories should only be concrete durable facts.
- Do not duplicate beliefs as memories.
- If unsure whether a belief is useful, return no beliefs.

Do NOT create beliefs about:
- document version control from a single document update
- generic CRUD safety from a single mutation
- user preferences inferred from required confirmation flow
- expense processing from one generated expense document
- internal runtime mechanics unless the user is explicitly designing that system

Do NOT create memories for:
- doc_id values
- file paths
- pending confirmations
- generated content
- temporary tool results
- one-off operational state

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

Observation pack:
{json.dumps(observation_pack or {}, ensure_ascii=False)[:22000]}

Scope context:
{json.dumps(scope_context or {}, ensure_ascii=False)[:4000]}

Research docs:
{json.dumps(research_docs or {}, ensure_ascii=False)[:16000]}

Existing context:
{json.dumps(existing_context or {}, ensure_ascii=False)[:12000]}

Previous tool results:
{json.dumps(previous_tool_results or {}, ensure_ascii=False)[:8000]}
""".strip()