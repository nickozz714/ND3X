"""Request/response models for Fabric Data Agent registrations.

Secrets (client_secret, bearer_token) are write-only: accepted on create/update,
never returned. Responses expose only `has_secret`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class FabricDataAgentBase(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    workspace_id: str
    data_agent_id: str
    api_version: Optional[str] = None
    auth_method: str = "azure_login"  # service_principal | azure_login | bearer_token
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    enabled: bool = True


class FabricDataAgentCreate(FabricDataAgentBase):
    client_secret: Optional[str] = None  # SP secret (encrypted at rest, never returned)
    bearer_token: Optional[str] = None   # stored token (encrypted at rest, never returned)


class FabricDataAgentUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    workspace_id: Optional[str] = None
    data_agent_id: Optional[str] = None
    api_version: Optional[str] = None
    auth_method: Optional[str] = None
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # set to update; "" clears; omit to keep
    bearer_token: Optional[str] = None   # set to update; "" clears; omit to keep
    enabled: Optional[bool] = None


class FabricDataAgentRead(FabricDataAgentBase):
    id: int
    has_secret: bool = False  # whether a client_secret/bearer_token is stored
