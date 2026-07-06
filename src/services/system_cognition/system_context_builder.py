from __future__ import annotations

from typing import Any, Dict, Optional

from component.logging import get_logger
from repository.system_cognition.memory_repository import MemoryRepository
from repository.system_cognition.belief_repository import BeliefRepository


log = get_logger(__name__)


class SystemContextBuilder:
    def __init__(
        self,
        *,
        memory_repo: MemoryRepository,
        belief_repo: BeliefRepository,
    ):
        log.debugx(
            "SystemContextBuilder initialiseren",
            has_memory_repo=memory_repo is not None,
            has_belief_repo=belief_repo is not None,
        )
        self.memory_repo = memory_repo
        self.belief_repo = belief_repo
        log.debugx("SystemContextBuilder geïnitialiseerd")

    def _format_belief_capsule(self, belief: Dict[str, Any]) -> Dict[str, Any]:
        insights = belief.get("insights") or []
        future_use = belief.get("future_use") or []

        if not insights and belief.get("content"):
            insights = [belief["content"]]

        return {
            "id": belief.get("id"),
            "topic": belief.get("topic"),
            "summary": belief.get("summary") or belief.get("content"),
            "insights": insights,
            "future_use": future_use or belief.get("use_when") or [],
            "confidence": belief.get("confidence"),
            "status": belief.get("status"),
            "importance": belief.get("importance"),
            "domain": belief.get("domain"),
            "evidence_refs": belief.get("evidence_refs") or [],
            "metadata_": belief.get("metadata_") or {},
        }

    async def build(
            self,
            *,
            query: str,
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            top_k_memories: int = 8,
            top_k_beliefs: int = 8,
    ) -> Dict[str, Any]:
        log.infox(
            "System context bouwen gestart",
            project_id=project_id,
            thread_id=thread_id,
            query_length=len(query or ""),
            top_k_memories=top_k_memories,
            top_k_beliefs=top_k_beliefs,
        )
        project_id = project_id or None
        memories = await self.memory_repo.search(
            query=query,
            thread_id=thread_id,
            project_id=project_id,
            limit=top_k_memories,
            include_global=True,
        )
        log.debugx(
            "System context memories opgehaald",
            thread_id=thread_id,
            project_id=project_id,
            memory_count=len(memories) if memories is not None else None,
            top_k_memories=top_k_memories,
            include_global=True,
        )

        beliefs = await self.belief_repo.search(
            query=query,
            thread_id=thread_id,
            project_id=project_id,
            limit=top_k_beliefs,
            include_global=True,
        )
        log.debugx(
            "System context beliefs opgehaald",
            thread_id=thread_id,
            project_id=project_id,
            belief_count=len(beliefs) if beliefs is not None else None,
            top_k_beliefs=top_k_beliefs,
            include_global=True,
        )

        belief_capsules = [
            self._format_belief_capsule(b)
            for b in (beliefs or [])
        ]

        result = {
            "memories": memories,
            "beliefs": belief_capsules,
            "raw_beliefs": beliefs,
            "instructions": {
                "memory_scope_order": ["thread", "project", "global"],
                "beliefs_are_tentative": True,
                "use_beliefs_as_heuristics_not_facts": True,
                "beliefs_are_rich_capsules": True,
            },
        }

        log.infox(
            "System context bouwen afgerond",
            project_id=project_id,
            thread_id=thread_id,
            memory_count=len(memories) if memories is not None else None,
            belief_count=len(beliefs) if beliefs is not None else None,
            instruction_keys=list(result["instructions"].keys()),
        )
        return result