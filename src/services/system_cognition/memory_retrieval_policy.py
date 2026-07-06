from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from component.config import settings
from component.logging import get_logger
from repository.system_cognition.memory_repository import MemoryRepository

log = get_logger(__name__)


ALLOWED_PLANNER_MEMORY_TYPES: Set[str] = {
    "user_preference",
    "project_memory",
    "architecture_decision",
    "implementation_detail",
    "correction",
}

ROUTER_MEMORY_TYPE = "router_memory"

MAX_PLANNER_MEMORIES = 3
MAX_ROUTER_MEMORIES = 3

MIN_VECTOR_SCORE_BY_SCOPE = {
    "thread": 0.42,
    "project": 0.48,
    "global": 0.62,
}

ROUTER_MEMORY_MIN_VECTOR_SCORE = 0.58


# Fallback lexical threshold is intentionally strict-ish and only used when
# embeddings are temporarily unavailable. Main path is vector/cosine.
MIN_FALLBACK_SCORE_BY_SCOPE = {
    "thread": 0.12,
    "project": 0.14,
    "global": 0.22,
}
ROUTER_MEMORY_MIN_FALLBACK_SCORE = 0.18


def _normalize_text(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _terms(value: str) -> List[str]:
    value = _normalize_text(value)
    raw = re.split(r"[^a-z0-9A-ZÀ-ÿ_]+", value)
    stop = {
        "the", "and", "or", "een", "van", "voor", "met", "het", "dat", "die", "deze",
        "this", "that", "with", "from", "naar", "over", "als", "dan", "maar", "niet",
    }
    return [term for term in raw if len(term) >= 3 and term not in stop][:32]


def _content_key(memory: Dict[str, Any]) -> str:
    return _normalize_text(str(memory.get("content") or ""))


class MemoryRetrievalPolicy:
    def __init__(self, *, memory_repo: MemoryRepository, openai_service):
        self.memory_repo = memory_repo
        self.openai = openai_service
        # None → the LLM router resolves the embeddings slot (registry). No config default.
        self.embedding_model = None
        self.embedding_dimensions = getattr(settings, "SYSTEM_COGNITION_EMBEDDING_DIMENSIONS", None)

    def _query_embedding(self, query: str) -> List[float]:
        return self.openai.embed(
            query,
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            normalize=True,
        )

    def _score_vector(self, *, query_embedding: List[float], memory: Dict[str, Any]) -> float:
        embedding = memory.get("embedding") or []
        if not embedding:
            return 0.0
        return round(float(self.openai.cosine_similarity(query_embedding, embedding)), 6)

    def _score_fallback_lexical(self, *, query: str, memory: Dict[str, Any]) -> float:
        query_terms = set(_terms(query))
        if not query_terms:
            return 0.0

        metadata = memory.get("metadata_") or {}
        content = _normalize_text(
            " ".join(
                [
                    str(memory.get("type") or ""),
                    str(memory.get("content") or ""),
                    " ".join(str(x) for x in metadata.get("tags", []) or []),
                    str(metadata.get("domain") or ""),
                ]
            )
        )

        hits = sum(1 for term in query_terms if term in content)
        lexical = hits / max(len(query_terms), 1)

        importance = float(memory.get("importance") or 0.0)
        pinned = bool(memory.get("pinned", False))
        scope = (memory.get("scope") or "global").strip().lower()

        boost = 0.0
        if scope == "thread":
            boost += 0.04
        elif scope == "project":
            boost += 0.03
        if pinned:
            boost += 0.06
        boost += min(max(importance, 0.0), 1.0) * 0.03

        return round(lexical + boost, 4)

    def _reject(
        self,
        rejected: List[Dict[str, Any]],
        memory: Dict[str, Any],
        *,
        reason: str,
        score: Optional[float] = None,
    ) -> None:
        rejected.append(
            {
                "id": memory.get("id"),
                "type": memory.get("type"),
                "scope": memory.get("scope"),
                "thread_id": memory.get("thread_id"),
                "project_id": memory.get("project_id"),
                "score": score,
                "reason": reason,
                "content_preview": str(memory.get("content") or "")[:220],
            }
        )

    async def retrieve_planner_candidates(
        self,
        *,
        query: str,
        real_thread_id: str,
        cognition_thread_id: Optional[str],
        project_id: Optional[str],
        requested_scopes: Sequence[str],
        requested_types: Sequence[str],
        limit: int = 250,
    ) -> Dict[str, Any]:
        effective_scopes = [
            str(scope).strip().lower()
            for scope in requested_scopes
            if str(scope).strip().lower() in {"thread", "project", "global"}
        ]

        if not project_id:
            effective_scopes = [scope for scope in effective_scopes if scope != "project"]

        if not cognition_thread_id:
            effective_scopes = [scope for scope in effective_scopes if scope != "thread"]

        if not effective_scopes:
            effective_scopes = ["global"]

        effective_types = [
            str(memory_type).strip()
            for memory_type in requested_types
            if str(memory_type).strip() in ALLOWED_PLANNER_MEMORY_TYPES
        ]

        if not effective_types:
            effective_types = ["user_preference", "correction"]

        include_global = "global" in effective_scopes
        search_thread_id = cognition_thread_id if "thread" in effective_scopes else None
        search_project_id = project_id if "project" in effective_scopes else None

        score_type = "cosine_embedding"
        try:
            query_embedding = await asyncio.to_thread(self._query_embedding, query)
            raw = await self.memory_repo.vector_candidates(
                thread_id=search_thread_id,
                project_id=search_project_id,
                include_global=include_global,
                types=effective_types,
                limit=limit,
            )
        except Exception as exc:
            log.warningx(
                "Vector planner memory retrieval failed; falling back to lexical search",
                error=repr(exc),
            )
            query_embedding = []
            score_type = "fallback_lexical"
            raw = await self.memory_repo.search(
                query=query,
                thread_id=search_thread_id,
                project_id=search_project_id,
                include_global=include_global,
                limit=limit,
            )

        kept: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        seen_content: set[str] = set()

        for memory in raw or []:
            memory_type = (memory.get("type") or "").strip()
            scope = (memory.get("scope") or "global").strip().lower()

            if memory_type == ROUTER_MEMORY_TYPE:
                self._reject(rejected, memory, reason="router_memory_not_allowed_for_planner")
                continue

            if memory_type not in effective_types:
                self._reject(rejected, memory, reason="type_not_requested")
                continue

            if scope not in effective_scopes:
                self._reject(rejected, memory, reason="scope_not_requested")
                continue

            if scope == "thread" and cognition_thread_id and memory.get("thread_id") != cognition_thread_id:
                self._reject(rejected, memory, reason="wrong_thread")
                continue

            if scope == "project" and project_id and memory.get("project_id") != project_id:
                self._reject(rejected, memory, reason="wrong_project")
                continue

            key = _content_key(memory)
            if key and key in seen_content:
                self._reject(rejected, memory, reason="duplicate_content")
                continue
            seen_content.add(key)

            if score_type == "cosine_embedding":
                score = self._score_vector(query_embedding=query_embedding, memory=memory)
                threshold = MIN_VECTOR_SCORE_BY_SCOPE.get(scope, 0.62)
            else:
                score = self._score_fallback_lexical(query=query, memory=memory)
                threshold = MIN_FALLBACK_SCORE_BY_SCOPE.get(scope, 0.22)

            if score < threshold:
                self._reject(rejected, memory, reason="score_below_threshold", score=score)
                continue

            enriched = dict(memory)
            enriched["_retrieval_score"] = score
            enriched["_retrieval_scope"] = scope
            enriched["_retrieval_score_type"] = score_type
            kept.append(enriched)

        kept = sorted(
            kept,
            key=lambda item: (
                float(item.get("_retrieval_score") or 0.0),
                bool(item.get("pinned", False)),
                float(item.get("importance") or 0.0),
            ),
            reverse=True,
        )[:MAX_PLANNER_MEMORIES]

        return {
            "memories": kept,
            "beliefs": [],
            "raw_beliefs": [],
            "instructions": {
                "source": "memory_retrieval_policy",
                "beliefs_disabled": True,
                "max_memories": MAX_PLANNER_MEMORIES,
                "effective_scopes": effective_scopes,
                "effective_types": effective_types,
                "score_type": score_type,
            },
            "_retrieval_debug": {
                "query": query,
                "score_type": score_type,
                "candidate_count": len(raw or []),
                "kept_count": len(kept),
                "rejected_count": len(rejected),
                "kept_ids": [item.get("id") for item in kept],
                "rejected_preview": rejected[:10],
            },
        }

    async def retrieve_router_candidates(
        self,
        *,
        query: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        score_type = "cosine_embedding"
        try:
            query_embedding = await asyncio.to_thread(self._query_embedding, query)
            raw = await self.memory_repo.vector_candidates(
                thread_id="cognition_router",
                project_id=None,
                include_global=False,
                types=[ROUTER_MEMORY_TYPE],
                limit=limit,
            )
        except Exception as exc:
            log.warningx(
                "Vector router memory retrieval failed; falling back to lexical search",
                error=repr(exc),
            )
            query_embedding = []
            score_type = "fallback_lexical"
            raw = await self.memory_repo.search(
                query=query,
                thread_id="cognition_router",
                project_id=None,
                limit=limit,
                include_global=False,
            )

        kept: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        seen_content: set[str] = set()

        for memory in raw or []:
            memory_type = (memory.get("type") or "").strip()
            thread_id = memory.get("thread_id")
            metadata = memory.get("metadata_") or {}

            is_router_memory = (
                memory_type == ROUTER_MEMORY_TYPE
                or thread_id == "cognition_router"
                or bool(metadata.get("router_memory"))
            )

            if not is_router_memory:
                self._reject(rejected, memory, reason="not_router_memory")
                continue

            if thread_id != "cognition_router":
                self._reject(rejected, memory, reason="wrong_router_thread")
                continue

            key = _content_key(memory)
            if key and key in seen_content:
                self._reject(rejected, memory, reason="duplicate_content")
                continue
            seen_content.add(key)

            if score_type == "cosine_embedding":
                score = self._score_vector(query_embedding=query_embedding, memory=memory)
                threshold = ROUTER_MEMORY_MIN_VECTOR_SCORE
            else:
                score = self._score_fallback_lexical(query=query, memory=memory)
                threshold = ROUTER_MEMORY_MIN_FALLBACK_SCORE

            if score < threshold:
                self._reject(rejected, memory, reason="score_below_threshold", score=score)
                continue

            enriched = dict(memory)
            enriched["_retrieval_score"] = score
            enriched["_retrieval_score_type"] = score_type
            kept.append(enriched)

        kept = sorted(
            kept,
            key=lambda item: (
                float(item.get("_retrieval_score") or 0.0),
                bool(item.get("pinned", False)),
                float(item.get("importance") or 0.0),
            ),
            reverse=True,
        )[:MAX_ROUTER_MEMORIES]

        return {
            "memories": kept,
            "beliefs": [],
            "raw_beliefs": [],
            "instructions": {
                "source": "router_memory_retrieval_policy",
                "router_memories_only": True,
                "max_memories": MAX_ROUTER_MEMORIES,
                "score_type": score_type,
            },
            "_retrieval_debug": {
                "query": query,
                "score_type": score_type,
                "candidate_count": len(raw or []),
                "kept_count": len(kept),
                "rejected_count": len(rejected),
                "kept_ids": [item.get("id") for item in kept],
                "rejected_preview": rejected[:10],
            },
        }
