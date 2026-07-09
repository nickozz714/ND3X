"""Tests for the ND3X MCP gateway: it re-exposes DB tools (builtins + MCP
servers like Fabric) to the CLI, excludes the web tools (the CLI has its own),
and generates a stdio --mcp-config."""
from __future__ import annotations

import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Full model registry so relationship strings resolve for the Tool query.
for _m in (
    "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
    "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
    "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
    "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
    "transfer", "meeting_profile", "slash_command", "secret", "board", "workflow",
):
    __import__(f"models.{_m}")

import models.tool as tool_model
import models.mcp_server as mcp_model
from db.database import Base
from services.mcp import mcp_gateway


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # get_all_with_relations eager-loads related tables (assistant_tool, …), so
    # create the whole schema the loaded models define.
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _server(db, name="Fabric", enabled=True):
    from datetime import datetime
    srv = mcp_model.MCPServer(
        name=name, slug=name.lower(), server_type="http", base_url="https://x",
        is_enabled=enabled, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(srv); db.commit()
    return srv


def _tool(db, server, name, enabled=True):
    from datetime import datetime
    t = tool_model.Tool(
        remote_name=name, name=name, description=f"{name} desc", argument={"type": "object", "properties": {}},
        type="tool", tool_instructions="", is_enabled=enabled, mcp_server_id=server.id,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(t); db.commit()
    return t


def test_gateway_lists_tools_excludes_web(db):
    srv = _server(db)
    _tool(db, srv, "fabric_list_workspaces")
    _tool(db, srv, "board__create")
    _tool(db, srv, "web_search")          # excluded — CLI has its own
    _tool(db, srv, "web_fetch")           # excluded
    _tool(db, srv, "disabled_tool", enabled=False)  # excluded — disabled

    names = [t.name for t in mcp_gateway._list_gateway_tools(db)]
    assert "fabric_list_workspaces" in names
    assert "board__create" in names
    assert "web_search" not in names and "web_fetch" not in names
    assert "disabled_tool" not in names


def test_gateway_skips_disabled_server(db):
    srv = _server(db, name="OffServer", enabled=False)
    _tool(db, srv, "fabric_tool_on_off_server")
    assert mcp_gateway._list_gateway_tools(db) == []


def test_tool_schema_normalization():
    assert mcp_gateway._tool_to_schema({"type": "object", "properties": {"a": {}}})["type"] == "object"
    # A bare properties dict is wrapped as an object schema.
    assert mcp_gateway._tool_to_schema({"properties": {"a": {}}})["type"] == "object"
    # Anything else → empty object schema.
    assert mcp_gateway._tool_to_schema(None) == {"type": "object", "properties": {}}


def test_mcp_config_for_cli_shape():
    cfg = mcp_gateway.mcp_config_for_cli(python="/usr/bin/python3")
    server = cfg["mcpServers"][mcp_gateway.SERVER_NAME]
    assert server["command"] == "/usr/bin/python3"
    assert server["args"] == ["-m", "services.mcp.mcp_gateway"]
    # Child is quieted so stdout stays pure JSON-RPC.
    assert server["env"]["LOG_LEVEL"] == "ERROR"
    assert server["env"]["LOG_DB_ENABLED"] == "false"
    assert "PYTHONPATH" in server["env"]
