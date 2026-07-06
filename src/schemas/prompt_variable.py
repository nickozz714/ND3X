from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class PromptVariableCreate(BaseModel):
    token: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=1)
    is_enabled: bool = True
    timeout_ms: int = 1000


class PromptVariableUpdate(BaseModel):
    token: Optional[str] = Field(None, min_length=1, max_length=100)
    code: Optional[str] = Field(None, min_length=1)
    is_enabled: Optional[bool] = None
    timeout_ms: Optional[int] = None


class PromptVariableRead(BaseModel):
    id: int
    token: str
    code: str
    is_enabled: bool
    timeout_ms: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True