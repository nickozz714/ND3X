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


# ---------------------------------------------------------- skill scoping


def _skill(db, name, enabled=True):
    from datetime import datetime
    import models.skill as skill_model
    s = skill_model.Skill(name=name, description=f"{name} desc", is_enabled=enabled,
                          created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(s); db.commit()
    return s


def _link(db, skill, tool, enabled=True):
    import models.skill_tool as st_model
    link = st_model.SkillTool(skill_id=skill.id, tool_id=tool.id, is_enabled=enabled)
    db.add(link); db.commit()
    return link


def test_gateway_none_selection_exposes_everything(db):
    srv = _server(db, "Builtin")
    t_free = _tool(db, srv, "board_pull")
    t_linked = _tool(db, srv, "workflow__generate")
    _link(db, _skill(db, "workflow_building"), t_linked)
    names = {t.name for t in mcp_gateway._list_gateway_tools(db, None)}
    assert {"board_pull", "workflow__generate"} <= names  # legacy: no filtering


def test_gateway_scopes_linked_tools_to_selected_skills(db):
    srv = _server(db, "Builtin")
    t_free = _tool(db, srv, "board_pull")
    t_wf = _tool(db, srv, "workflow__generate")
    t_transfer = _tool(db, srv, "transfer_create_route")
    _link(db, _skill(db, "workflow_building"), t_wf)
    _link(db, _skill(db, "transfer_route_building"), t_transfer)

    # No skills selected → only unlinked tools.
    names = {t.name for t in mcp_gateway._list_gateway_tools(db, set())}
    assert "board_pull" in names
    assert "workflow__generate" not in names and "transfer_create_route" not in names

    # workflow skill selected → its tools appear; the other skill's stay hidden.
    names = {t.name for t in mcp_gateway._list_gateway_tools(db, {"workflow_building"})}
    assert {"board_pull", "workflow__generate"} <= names
    assert "transfer_create_route" not in names


def test_gateway_disabled_link_or_skill_means_always_on(db):
    """A DISABLED link/skill doesn't scope the tool — it stays always-on rather
    than silently vanishing."""
    srv = _server(db, "Builtin")
    t1 = _tool(db, srv, "tool_disabled_link")
    t2 = _tool(db, srv, "tool_disabled_skill")
    _link(db, _skill(db, "some_skill"), t1, enabled=False)
    _link(db, _skill(db, "off_skill", enabled=False), t2)
    names = {t.name for t in mcp_gateway._list_gateway_tools(db, set())}
    assert {"tool_disabled_link", "tool_disabled_skill"} <= names


def test_selected_skills_env_parsing(monkeypatch):
    monkeypatch.delenv("ND3X_GATEWAY_SKILLS", raising=False)
    assert mcp_gateway._selected_skills_from_env() is None      # absent → no filtering
    monkeypatch.setenv("ND3X_GATEWAY_SKILLS", "")
    assert mcp_gateway._selected_skills_from_env() == set()      # empty → strict
    monkeypatch.setenv("ND3X_GATEWAY_SKILLS", "workflow_building, transfer_route_building")
    assert mcp_gateway._selected_skills_from_env() == {"workflow_building", "transfer_route_building"}


def test_mcp_config_for_cli_carries_selected_skills():
    cfg = mcp_gateway.mcp_config_for_cli(selected_skills=["workflow_building"])
    env = cfg["mcpServers"]["nd3x"]["env"]
    assert env["ND3X_GATEWAY_SKILLS"] == "workflow_building"
    cfg2 = mcp_gateway.mcp_config_for_cli()  # None → var absent → legacy
    assert "ND3X_GATEWAY_SKILLS" not in cfg2["mcpServers"]["nd3x"]["env"]
