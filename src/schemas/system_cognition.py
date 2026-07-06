from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class MemoryOut(BaseModel):
    id: str
    type: str
    content: str
    scope: str
    thread_id: Optional[str]
    importance: float
    pinned: bool
    metadata_: Dict[str, Any]
    created_at: str
    updated_at: str


class BeliefOut(BaseModel):
    id: str
    topic: str
    content: str
    summary: Optional[str]
    insights: List[Any]
    future_use: List[Any]
    domain: Optional[str]
    confidence: float
    status: str
    importance: float
    scope: str
    thread_id: Optional[str]
    use_when: List[Any]
    evidence_refs: List[Any]
    contradictions: List[Any]
    metadata_: Dict[str, Any]
    created_at: str
    updated_at: str
    last_verified_at: Optional[str]


class CuriosityJobOut(BaseModel):
    id: str
    topic: str
    reason: Optional[str]
    depth: str
    priority: float
    status: str
    thread_id: Optional[str]
    source_question: Optional[str]
    source_answer: Optional[str]
    attempts: int
    error: Optional[str]
    result: Dict[str, Any]
    metadata_: Dict[str, Any]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    updated_at: str


class PaginatedResponse(BaseModel):
    items: list
    total: int
    limit: int
    offset: int