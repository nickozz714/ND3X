from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    display_name: Optional[str] = None
    description: str = ""
    instructions: str = ""

    input_schema: Optional[dict[str, Any]] = None
    output_schema: Optional[dict[str, Any]] = None

    is_system: bool = False
    is_runtime: bool = False
    is_enabled: bool = True
    priority: int = 100

    source: str = "local"
    source_name: Optional[str] = None
    version: str = "1.0.0"

    # Free-text organisational tags (Skills overview filtering). Not used for selection.
    routing_tags: list[str] = Field(default_factory=list)

    @field_validator("routing_tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v):
        # DB stores NULL for legacy rows; treat NULL/empty as an empty list.
        return v or []


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None

    input_schema: Optional[dict[str, Any]] = None
    output_schema: Optional[dict[str, Any]] = None

    is_system: Optional[bool] = None
    is_runtime: Optional[bool] = None
    is_enabled: Optional[bool] = None
    priority: Optional[int] = None

    source: Optional[str] = None
    source_name: Optional[str] = None
    version: Optional[str] = None

    routing_tags: Optional[list[str]] = None


class SkillRead(SkillBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SkillMarkdownImportFile(BaseModel):
    relative_path: str = Field(..., min_length=1, max_length=512)
    content: str = ""
    content_type: Optional[str] = None
    is_editable: bool = True
    is_executable: bool = False


class SkillMarkdownImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    markdown: str = Field(..., min_length=1)
    source_name: Optional[str] = None
    is_system: bool = False
    is_runtime: bool = False
    priority: int = 100
    files: list[SkillMarkdownImportFile] = Field(default_factory=list)


class AssistantSkillLinkRequest(BaseModel):
    skill_id: int


class SkillToolLinkRequest(BaseModel):
    tool_id: int


class SkillToolMiniResponse(BaseModel):
    id: int
    mcp_server_id: int
    remote_name: str
    name: str
    description: str
    argument: Any
    output_schema: Optional[Any] = None
    annotations: Optional[Any] = None
    meta: Optional[Any] = None
    type: str
    tool_instructions: str
    is_dynamic_micro_tool: Optional[bool] = None
    attached_microservice: Optional[str] = None
    is_enabled: Optional[bool] = True
    availability_scope: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SkillAssistantMiniResponse(BaseModel):
    id: int
    name: str
    description: str
    assistant_type: str
    routing_tags: list[str] = Field(default_factory=list)
    priority: int = 100
    is_active: bool = True
    is_router_selectable: bool = True

    model_config = ConfigDict(from_attributes=True)


class SkillWithRelations(SkillRead):
    tools: list[SkillToolMiniResponse] = Field(default_factory=list)
    assistants: list[SkillAssistantMiniResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class SkillFilePayload(BaseModel):
    relative_path: str = Field(..., min_length=1, max_length=512)
    content: str = ""
    content_type: Optional[str] = None
    is_editable: bool = True
    is_executable: bool = False


class SkillFileUpdatePayload(BaseModel):
    relative_path: Optional[str] = Field(default=None, min_length=1, max_length=512)
    content: str = ""
    content_type: Optional[str] = None
    is_editable: Optional[bool] = None
    is_executable: Optional[bool] = None


class SkillFileRead(BaseModel):
    id: int
    skill_id: int
    relative_path: str
    filename: Optional[str] = None
    runtime_path: str
    content_type: Optional[str] = None
    size_bytes: int
    checksum_sha256: str
    is_editable: bool
    is_executable: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SkillFileDetail(SkillFileRead):
    content: str = ""
