from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from models.board import BOARD_ORIGINS, BOARD_PRIORITIES, BOARD_STATUSES


class BoardItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = None
    status: str = "todo"
    priority: str = "medium"
    acceptance: Optional[str] = None
    depends_on: List[int] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
    origin: str = "user"


class BoardItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    acceptance: Optional[str] = None
    depends_on: Optional[List[int]] = None
    labels: Optional[List[str]] = None
    result: Optional[str] = None
    position: Optional[int] = None


class BoardItemRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: str
    priority: str
    acceptance: Optional[str] = None
    depends_on: List[int] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
    origin: str
    updated_by: str
    result: Optional[str] = None
    position: int
    workflow_run_id: Optional[int] = None
    thread_id: Optional[str] = None
    # True when every dependency is done (or there are none) — the item can be
    # worked. A todo item that isn't ready is effectively blocked.
    ready: bool = True
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Exposed for validation error messages / the router.
VALID_STATUSES = BOARD_STATUSES
VALID_PRIORITIES = BOARD_PRIORITIES
VALID_ORIGINS = BOARD_ORIGINS
