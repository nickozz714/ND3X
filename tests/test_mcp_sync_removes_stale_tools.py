"""MCP tool sync deletes tools that vanished from the server (and cascades skill links
via tool_repo.delete) instead of just disabling them."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from services.mcp.mcp_server_sync_service import MCPServerSyncService


class _FakeClient:
    async def list_tools_listing(self):
        return {"tools": [{"name": "kept_tool", "description": "d", "inputSchema": {}}]}


def test_sync_deletes_vanished_tools():
    svc = MCPServerSyncService(db=SimpleNamespace(commit=lambda: None))
    server = SimpleNamespace(id=1, is_enabled=True, name="X", base_url=None, server_type="http")
    kept = SimpleNamespace(id=10, name="kept_tool", remote_name="kept_tool")
    stale = SimpleNamespace(id=11, name="old_tool", remote_name="old_tool")
    deleted: list[int] = []

    svc.server_repo = SimpleNamespace(get_by_id=lambda i: server, get_with_relations=lambda i: server)
    svc.auth_repo = SimpleNamespace(get_active_for_server=lambda i: None)
    svc.tool_repo = SimpleNamespace(
        get_by_server_and_remote_name=lambda mcp_server_id, remote_name: kept if remote_name == "kept_tool" else None,
        update=lambda i, obj: None,
        create=lambda obj: None,
        get_all_for_server=lambda i: [kept, stale],
        delete=lambda i: deleted.append(i),
    )
    svc.client_factory = SimpleNamespace(build=lambda **kw: _FakeClient())

    asyncio.run(svc.sync_server_tools(1))

    assert deleted == [11]              # only the vanished tool is removed
    assert server.last_sync_status == "success"
