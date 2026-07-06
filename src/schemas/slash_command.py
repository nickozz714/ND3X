from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

_NAME_HELP = "lowercase letters, digits, '-' or '_' (no leading slash)"


def _validate_name(v: str) -> str:
    name = (v or "").strip().lstrip("/").lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(f"Invalid command name — use {_NAME_HELP}.")
    return name


class SlashCommandBase(BaseModel):
    name: str
    description: str = ""
    template: str
    is_enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _validate_name(v)


class SlashCommandCreate(SlashCommandBase):
    pass


class SlashCommandUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    template: Optional[str] = None
    is_enabled: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return _validate_name(v) if v is not None else None


class SlashCommandRead(SlashCommandBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
