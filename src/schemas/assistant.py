from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantBase(BaseModel):
    name: str = Field(..., min_length=1)
    description: str
    instruction: str
    schema: Any

    assistant_type: str = Field(..., description="router | planner | final_answer")
    routing_tags: list[str] = Field(default_factory=list)

    @field_validator("routing_tags", mode="before")
    @classmethod
    def _routing_tags_none_to_list(cls, v):
        # The DB column is nullable, so a row can have NULL routing_tags. Coerce it to
        # [] instead of failing response serialization with a 500.
        return v if v is not None else []

    model: Optional[str] = None
    temperature: Optional[float] = None
    priority: int = 100
    is_router_selectable: bool = True
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class AssistantCreate(AssistantBase):
    pass


class AssistantUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instruction: Optional[str] = None
    schema: Optional[Any] = None

    assistant_type: Optional[str] = None
    routing_tags: Optional[list[str]] = None

    model: Optional[str] = None
    temperature: Optional[float] = None
    priority: Optional[int] = None
    is_router_selectable: Optional[bool] = None
    is_active: Optional[bool] = None


class AssistantResponse(AssistantBase):
    id: int
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MCPServerMiniResponse(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AssistantToolMiniResponse(BaseModel):
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
    mcp_server: Optional[MCPServerMiniResponse] = None

    model_config = ConfigDict(from_attributes=True)

class AssistantSkillMiniResponse(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    description: str = ""
    instructions: str = ""

    input_schema: Optional[Any] = None
    output_schema: Optional[Any] = None

    is_system: bool = False
    is_enabled: bool = True
    priority: int = 100

    source: str = "local"
    source_name: Optional[str] = None
    version: str = "1.0.0"

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class AssistantWithRelations(AssistantResponse):
    # Legacy/direct tools
    tools: list[AssistantToolMiniResponse] = Field(default_factory=list)

    # New runtime capabilities
    skills: list[AssistantSkillMiniResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)