from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryRecord:
    id: str = field(default_factory=lambda: str(uuid4()))
    type: str = "note"
    content: str = ""
    scope: str = "global"
    thread_id: Optional[str] = None
    project_id: Optional[str] = None
    embedding: Optional[List[float]] = None
    embedding_model: Optional[str] = None
    embedding_hash: Optional[str] = None
    embedding_updated_at: Optional[str] = None
    importance: float = 0.5
    pinned: bool = False
    metadata_: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BeliefRecord:
    id: str = field(default_factory=lambda: str(uuid4()))
    topic: str = ""
    content: str = ""
    summary: Optional[str] = None

    insights: List[str] = field(default_factory=list)
    future_use: List[str] = field(default_factory=list)
    domain: Optional[str] = None
    confidence: float = 0.5
    status: str = "tentative"
    importance: float = 0.5
    scope: str = "global"
    thread_id: Optional[str] = None
    project_id: Optional[str] = None
    embedding: Optional[List[float]] = None
    embedding_model: Optional[str] = None
    embedding_hash: Optional[str] = None
    embedding_updated_at: Optional[str] = None
    use_when: List[str] = field(default_factory=list)
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    metadata_: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_verified_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CuriosityJob:
    id: str = field(default_factory=lambda: str(uuid4()))
    topic: str = ""
    reason: str = ""
    depth: str = "small"
    priority: float = 0.5
    status: str = "queued"
    scope: str = "thread"
    project_id: Optional[str] = None
    thread_id: Optional[str] = None
    source_question: Optional[str] = None
    source_answer: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    result: Dict[str, Any] = field(default_factory=dict)
    metadata_: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)