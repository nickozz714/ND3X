"""
schemas/background_task.py

DTO's voor het achtergrondtaken-paneel in de workbench.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class BackgroundTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    owner_thread: Optional[str] = None
    assistant: Optional[str] = None
    task_preview: Optional[str] = None
    created_at: Optional[int] = None
    finished_at: Optional[int] = None
    result: Optional[Any] = None
    acknowledged: bool = False
