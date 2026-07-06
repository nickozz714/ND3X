from __future__ import annotations

from typing import Any, Dict, List


def _take_list(value: Any, limit: int = 8) -> List[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def compact_existing_context(existing_context: Dict[str, Any], *, max_memories: int = 4, max_beliefs: int = 4) -> Dict[str, Any]:
    if not isinstance(existing_context, dict):
        return {}

    memories = existing_context.get("memories") or []
    beliefs = existing_context.get("beliefs") or []

    return {
        "memories": memories[:max_memories] if isinstance(memories, list) else [],
        "beliefs": beliefs[:max_beliefs] if isinstance(beliefs, list) else [],
        "instructions": existing_context.get("instructions") or {},
    }


def compact_interpretation_for_memory(turn_interpretation: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(turn_interpretation, dict):
        return {}

    return {
        "turn_summary": turn_interpretation.get("turn_summary"),
        "candidate_memories": _take_list(turn_interpretation.get("candidate_memories"), 10),
        "architecture_decisions": _take_list(turn_interpretation.get("architecture_decisions"), 8),
        "implementation_details": _take_list(turn_interpretation.get("implementation_details"), 8),
        "user_preferences": _take_list(turn_interpretation.get("user_preferences"), 8),
        "constraints": _take_list(turn_interpretation.get("constraints"), 8),
        "corrections": _take_list(turn_interpretation.get("corrections"), 8),
        "project_context": _take_list(turn_interpretation.get("project_context"), 8),
        "agent_behavior_implications": _take_list(turn_interpretation.get("agent_behavior_implications"), 8),
    }


def compact_interpretation_for_curiosity(turn_interpretation: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(turn_interpretation, dict):
        return {}

    return {
        "turn_summary": turn_interpretation.get("turn_summary"),
        "main_topics": _take_list(turn_interpretation.get("main_topics"), 8),
        "technical_concepts": _take_list(turn_interpretation.get("technical_concepts"), 8),
        "researchworthy_topics": _take_list(turn_interpretation.get("researchworthy_topics"), 8),
        "worldview_seeds": _take_list(turn_interpretation.get("worldview_seeds"), 8),
        "candidate_belief_seeds": _take_list(turn_interpretation.get("candidate_belief_seeds"), 8),
        "tradeoffs": _take_list(turn_interpretation.get("tradeoffs"), 8),
        "open_questions": _take_list(turn_interpretation.get("open_questions"), 8),
        "architecture_decisions": _take_list(turn_interpretation.get("architecture_decisions"), 6),
        "constraints": _take_list(turn_interpretation.get("constraints"), 6),
    }


def compact_interpretation_for_belief(turn_interpretation: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(turn_interpretation, dict):
        return {}

    return {
        "turn_summary": turn_interpretation.get("turn_summary"),
        "main_topics": _take_list(turn_interpretation.get("main_topics"), 6),
        "technical_concepts": _take_list(turn_interpretation.get("technical_concepts"), 6),
        "worldview_seeds": _take_list(turn_interpretation.get("worldview_seeds"), 8),
        "candidate_belief_seeds": _take_list(turn_interpretation.get("candidate_belief_seeds"), 8),
        "tradeoffs": _take_list(turn_interpretation.get("tradeoffs"), 8),
        "architecture_decisions": _take_list(turn_interpretation.get("architecture_decisions"), 6),
        "constraints": _take_list(turn_interpretation.get("constraints"), 6),
        "agent_behavior_implications": _take_list(turn_interpretation.get("agent_behavior_implications"), 6),
    }


def compact_research_docs_for_belief(research_docs: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(research_docs, dict):
        return {}

    tool_results = research_docs.get("tool_results") or []
    compact_results = []

    if isinstance(tool_results, list):
        for result in tool_results[:2]:
            if not isinstance(result, dict):
                continue

            results = result.get("results") or []
            compact_results.append({
                "ok": result.get("ok"),
                "query": result.get("query"),
                "results": [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "summary": r.get("summary"),
                        "text": (r.get("text") or "")[:1200],
                    }
                    for r in results[:4]
                    if isinstance(r, dict)
                ],
            })

    return {
        "research_result": research_docs.get("research_result"),
        "tool_calls": research_docs.get("tool_calls") or [],
        "tool_results": compact_results,
    }