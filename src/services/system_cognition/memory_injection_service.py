from __future__ import annotations

from typing import Any, Dict, List

from component.logging import get_logger
from repository.system_cognition.memory_injection_repository import MemoryInjectionRepository


log = get_logger(__name__)


class MemoryInjectionService:
    def __init__(self):
        self.repository = MemoryInjectionRepository()

    def _compact_memory(self, item: Dict[str, Any], *, max_chars: int = 900) -> Dict[str, Any]:
        content = str(item.get("content") or "").strip()

        return {
            "id": item.get("id"),
            "type": item.get("type"),
            "scope": item.get("scope"),
            "thread_id": item.get("thread_id"),
            "project_id": item.get("project_id"),
            "importance": item.get("importance"),
            "pinned": bool(item.get("pinned", False)),
            "content": content[:max_chars],
        }

    def _compact_belief(self, item: Dict[str, Any], *, max_chars: int = 900) -> Dict[str, Any]:
        summary = str(item.get("summary") or "").strip()
        content = str(item.get("content") or "").strip()

        return {
            "id": item.get("id"),
            "topic": item.get("topic"),
            "scope": item.get("scope"),
            "thread_id": item.get("thread_id"),
            "project_id": item.get("project_id"),
            "confidence": item.get("confidence"),
            "importance": item.get("importance"),
            "status": item.get("status"),
            "domain": item.get("domain"),
            "summary": summary[:max_chars],
            "content": content[:max_chars] if not summary else "",
            "future_use": (item.get("future_use") or [])[:5] if isinstance(item.get("future_use"), list) else [],
            "use_when": (item.get("use_when") or [])[:5] if isinstance(item.get("use_when"), list) else [],
        }

    async def build_planner_context(
        self,
        *,
        real_thread_id: str,
        raw_context: Dict[str, Any],
        max_memories: int = 8,
        max_beliefs: int = 8,
    ) -> Dict[str, Any]:
        used = await self.repository.get_injected_ids(thread_id=real_thread_id)

        used_memory_ids = used.get("memory", set())
        used_belief_ids = used.get("belief", set())

        memories: List[Dict[str, Any]] = []
        beliefs: List[Dict[str, Any]] = []
        newly_injected = []

        for memory in raw_context.get("memories") or []:
            if not isinstance(memory, dict):
                continue

            memory_id = memory.get("id")
            if not memory_id or memory_id in used_memory_ids:
                continue

            memories.append(self._compact_memory(memory))
            newly_injected.append({
                "memory_kind": "memory",
                "memory_id": memory_id,
            })

            if len(memories) >= max_memories:
                break

        raw_beliefs = raw_context.get("raw_beliefs") or raw_context.get("beliefs") or []

        for belief in raw_beliefs:
            if not isinstance(belief, dict):
                continue

            belief_id = belief.get("id")
            if not belief_id or belief_id in used_belief_ids:
                continue

            beliefs.append(self._compact_belief(belief))
            newly_injected.append({
                "memory_kind": "belief",
                "memory_id": belief_id,
            })

            if len(beliefs) >= max_beliefs:
                break

        if newly_injected:
            await self.repository.mark_injected(
                thread_id=real_thread_id,
                items=newly_injected,
            )

        return {
            "memories": memories,
            "beliefs": beliefs,
            "instructions": raw_context.get("instructions") or {
                "memory_scope_order": ["thread", "project", "global"],
                "beliefs_are_tentative": True,
                "use_beliefs_as_heuristics_not_facts": True,
            },
            "_injected_ids": newly_injected,
        }

    async def build_router_context(
        self,
        *,
        real_thread_id: str,
        raw_context: Dict[str, Any],
        max_memories: int = 6,
        max_beliefs: int = 4,
    ) -> Dict[str, Any]:
        """
        Router memories also use the same injected-once-per-thread table.

        Router memories should be stored separately, usually:
          thread_id = "cognition_router"
          type = "router_memory"
        """
        used = await self.repository.get_injected_ids(thread_id=real_thread_id)

        used_memory_ids = used.get("memory", set())
        used_belief_ids = used.get("belief", set())

        memories: List[Dict[str, Any]] = []
        beliefs: List[Dict[str, Any]] = []
        newly_injected = []

        for memory in raw_context.get("memories") or []:
            if not isinstance(memory, dict):
                continue

            memory_id = memory.get("id")
            if not memory_id or memory_id in used_memory_ids:
                continue

            memories.append(self._compact_memory(memory))
            newly_injected.append({
                "memory_kind": "memory",
                "memory_id": memory_id,
            })

            if len(memories) >= max_memories:
                break

        raw_beliefs = raw_context.get("raw_beliefs") or raw_context.get("beliefs") or []

        for belief in raw_beliefs:
            if not isinstance(belief, dict):
                continue

            belief_id = belief.get("id")
            if not belief_id or belief_id in used_belief_ids:
                continue

            beliefs.append(self._compact_belief(belief))
            newly_injected.append({
                "memory_kind": "belief",
                "memory_id": belief_id,
            })

            if len(beliefs) >= max_beliefs:
                break

        if newly_injected:
            await self.repository.mark_injected(
                thread_id=real_thread_id,
                items=newly_injected,
            )

        return {
            "memories": memories,
            "beliefs": beliefs,
            "instructions": raw_context.get("instructions") or {
                "purpose": "Use router memories only to improve assistant/workflow selection.",
            },
            "_injected_ids": newly_injected,
        }