from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Any, Dict

from fastapi import APIRouter, status
from routers._mcp_proxy import mcp_proxy_call, mcp_proxy_health
from pydantic import BaseModel, Field

from component.config import settings
from services.mcp.mcp_client import MCPClient

router = APIRouter(prefix="/admin/keyvault", tags=["admin-keyvault"])
mcp = MCPClient(mcp_url=settings.MCP_URL, bearer=settings.MCP_BEARER)


# ---------------------------------------------------------------------
# Schemas mirrored from KeyVault
# ---------------------------------------------------------------------

class SecretCreateRequest(BaseModel):
    name: str
    value: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    placeholder: bool = False


class SecretUpdateRequest(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    placeholder: Optional[bool] = None


class SecretMetadataResponse(BaseModel):
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    placeholder: bool
    has_value: bool
    created_at: datetime
    updated_at: datetime


class DeleteResponse(BaseModel):
    ok: bool
    deleted: str


class SecretValueObfuscatedResponse(BaseModel):
    name: str
    value_obfuscated: str
    has_value: bool = True


# ---------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------

SERVICE_NAME = "keyvault"
HEALTH_TOOL = "keyvault_health"


async def _call(tool_name: str, payload: Dict[str, Any]) -> Any:
    return await mcp_proxy_call(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=tool_name,
        payload=payload,
    )

# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

@router.get("/health")
async def keyvault_health() -> Dict[str, Any]:
    return await mcp_proxy_health(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=HEALTH_TOOL,
    )

# ---------------------------------------------------------------------
# CRUD routes via MCP
# ---------------------------------------------------------------------

@router.post(
    "/secrets",
    response_model=SecretMetadataResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_secret(payload: SecretCreateRequest):
    """
    Create a secret in KeyVault through the MCP server.
    """
    data = await _call("keyvault_secret_create", payload.model_dump())
    return SecretMetadataResponse.model_validate(data)


@router.get(
    "/secrets",
    response_model=list[SecretMetadataResponse],
)
async def list_secrets():
    """
    List KeyVault secret metadata through the MCP server.
    Does not return raw secret values.
    """
    data = await _call("keyvault_secret_list", {})
    return [SecretMetadataResponse.model_validate(item) for item in data]


@router.get(
    "/secrets/{name}",
    response_model=SecretMetadataResponse,
)
async def get_secret(name: str):
    """
    Get one secret's metadata by name through the MCP server.
    Does not return the raw secret value.
    """
    data = await _call("keyvault_secret_get", {"name": name})
    return SecretMetadataResponse.model_validate(data)


@router.put(
    "/secrets/{name}",
    response_model=SecretMetadataResponse,
)
async def update_secret(name: str, payload: SecretUpdateRequest):
    """
    Update a secret in KeyVault through the MCP server.
    """
    data = await _call(
        "keyvault_secret_update",
        {"name": name, **payload.model_dump(exclude_none=True)},
    )
    return SecretMetadataResponse.model_validate(data)


@router.delete(
    "/secrets/{name}",
    response_model=DeleteResponse,
)
async def delete_secret(name: str):
    """
    Delete a secret from KeyVault through the MCP server.
    """
    data = await _call("keyvault_secret_delete", {"name": name})
    return DeleteResponse.model_validate(data)


# ---------------------------------------------------------------------
# Obfuscated value route via MCP
# ---------------------------------------------------------------------

@router.get(
    "/secrets/{name}/value",
    response_model=SecretValueObfuscatedResponse,
)
async def get_secret_value_obfuscated(name: str):
    """
    Get a secret value in obfuscated form through the MCP server.

    The router never talks directly to KeyVault and never receives the raw
    secret value. Obfuscation happens inside the MCP tool.
    """
    data = await _call("keyvault_secret_value_obfuscated", {"name": name})
    return SecretValueObfuscatedResponse.model_validate(data)
