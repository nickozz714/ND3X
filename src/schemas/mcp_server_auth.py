from __future__ import annotations

from datetime import datetime
from typing import Optional, Any

from pydantic import BaseModel, ConfigDict


class MCPServerAuthBase(BaseModel):
    auth_type: str
    token: Optional[str] = None
    config: Optional[Any] = None
    is_active: bool = True


class MCPServerAuthCreate(MCPServerAuthBase):
    mcp_server_id: int


class MCPServerAuthUpsert(BaseModel):
    auth_type: str
    token: Optional[str] = None
    config: Optional[Any] = None
    is_active: bool = True


class MCPServerAuthUpdate(BaseModel):
    auth_type: Optional[str] = None
    token: Optional[str] = None
    config: Optional[Any] = None
    is_active: Optional[bool] = None


class MCPServerAuthResponse(BaseModel):
    id: int
    mcp_server_id: int
    auth_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)