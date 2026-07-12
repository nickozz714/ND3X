"""Dynamic ND3X inventory block for CLI-agent turns (#13): connected MCP servers
+ skill catalog + selected skills' file roots. Complements the static preamble."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

for _m in (
    "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
    "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
    "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
    "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
    "transfer", "meeting_profile", "slash_command", "secret", "board", "workflow",
):
    __import__(f"models.{_m}")

import models.mcp_server as mc
import models.skill as sk
from db.database import Base
from services.providers.nd3x_agent_context import build_nd3x_context_block


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _skill(db, name, desc, **kw):
    row = sk.Skill(name=name, description=desc, instructions="x",
                   created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **kw)
    db.add(row); db.commit()
    return row


def _server(db, name, server_type="stdio", enabled=True):
    row = mc.MCPServer(name=name, slug=name.lower().replace(" ", "-"), server_type=server_type,
                       is_enabled=enabled, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(row); db.commit()
    return row


def test_lists_servers_and_skill_catalog(db):
    _server(db, "Fabric MCP Server")
    _server(db, "Builtin", server_type="builtin")       # excluded (implicit)
    _server(db, "Disabled One", enabled=False)          # excluded (disabled)
    _skill(db, "fabric_ops", "Query Fabric")
    _skill(db, "sys_skill", "system", is_system=True)   # excluded
    _skill(db, "rt_skill", "runtime", is_runtime=True)  # excluded

    block = build_nd3x_context_block(db)
    assert "Fabric MCP Server" in block
    assert "Builtin" not in block
    assert "Disabled One" not in block
    assert "fabric_ops: Query Fabric" in block
    assert "sys_skill" not in block
    assert "rt_skill" not in block


def test_empty_when_nothing(db):
    assert build_nd3x_context_block(db) == ""
