from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class MeetingProfileBase(BaseModel):
    name: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    language: Optional[str] = None
    output_template: Optional[str] = None
    # Meeting-driven actions (#9) policy. Null/absent = actions disabled (opt-in).
    action_policy: Optional[Dict[str, Any]] = None
    enabled: bool = True
    is_default: bool = False


class MeetingProfileCreate(MeetingProfileBase):
    pass


class MeetingProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    language: Optional[str] = None
    output_template: Optional[str] = None
    action_policy: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None


class MeetingProfileRead(MeetingProfileBase):
    id: int


class AvailableProfile(BaseModel):
    """A selectable meeting profile (code-defined or DB-defined), for the picker."""
    id: str
    name: str
    source: str  # "builtin" | "custom"


class MeetingProfileTemplate(BaseModel):
    """A starter template the user can create a profile from (guidance)."""
    key: str
    name: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    language: Optional[str] = None
    output_template: Optional[str] = None
    action_policy: Optional[Dict[str, Any]] = None
