"""When MCP_* is not configured, the MCP-backed health endpoints must report the
capability as not working (503 mcp_not_configured) instead of crashing."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from routers._mcp_proxy import mcp_proxy_health, mcp_proxy_call
from services.mcp.mcp_client import MCPClient


def test_health_reports_not_configured_when_mcp_unset():
    mcp = MCPClient(mcp_url="", bearer="")  # no MCP_URL/MCP_BEARER configured
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mcp_proxy_health(mcp=mcp, service="keyvault", tool="keyvault_health"))
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "mcp_not_configured"
    assert exc.value.detail["available"] is False


def test_call_reports_not_configured_when_mcp_unset():
    mcp = MCPClient(mcp_url="", bearer="")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mcp_proxy_call(mcp=mcp, service="pm", tool="pm_project_list", payload={}))
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "mcp_not_configured"
