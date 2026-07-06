from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class MCPServerMiniResponse(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ToolBase(BaseModel):
    mcp_server_id: int
    remote_name: str
    name: str = Field(..., min_length=1)
    description: str
    argument: Any
    output_schema: Optional[Any] = None
    annotations: Optional[Any] = None
    meta: Optional[Any] = None
    type: str
    tool_instructions: str

    is_dynamic_micro_tool: Optional[bool] = None
    attached_microservice: Optional[str] = None

    is_enabled: bool = True
    availability_scope: Optional[str] = None


class ToolCreate(ToolBase):
    pass


class ToolUpdate(BaseModel):
    mcp_server_id: Optional[int] = None
    remote_name: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    argument: Optional[Any] = None
    output_schema: Optional[Any] = None
    annotations: Optional[Any] = None
    meta: Optional[Any] = None
    type: Optional[str] = None
    tool_instructions: Optional[str] = None

    is_dynamic_micro_tool: Optional[bool] = None
    attached_microservice: Optional[str] = None

    is_enabled: Optional[bool] = None
    availability_scope: Optional[str] = None


class ToolResponse(ToolBase):
    id: int
    created_at: datetime
    updated_at: datetime
    mcp_server: Optional[MCPServerMiniResponse] = None

    model_config = ConfigDict(from_attributes=True)


class AssistantMiniResponse(BaseModel):
    id: int
    name: str
    description: str
    assistant_type: str
    routing_tags: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    temperature: Optional[float] = None
    priority: int = 100
    is_router_selectable: bool = True
    is_active: bool = True
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ToolWithRelations(ToolResponse):
    assistants: list[AssistantMiniResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)