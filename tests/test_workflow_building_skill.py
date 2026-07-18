"""The workflow-building capability for the CLI agent: the workflow__generate /
workflow__describe builtin tools and the seeded 'workflow_building' skill that
links them (so the gateway skill-scopes them to relevant turns)."""
from __future__ import annotations

import asyncio

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

from db.database import Base
import models.mcp_server as mcp_model
import models.skill as skill_model
import models.skill_tool as st_model
import models.tool as tool_model


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _builtin_server(db):
    from datetime import datetime
    srv = mcp_model.MCPServer(name="Builtin", slug="builtin", server_type="builtin",
                              is_enabled=True, created_at=datetime.utcnow(),
                              updated_at=datetime.utcnow())
    db.add(srv); db.commit()
    return srv


def _tool(db, server, name):
    from datetime import datetime
    t = tool_model.Tool(remote_name=name, name=name, description=f"{name} desc",
                        argument={"type": "object", "properties": {}}, type="tool",
                        tool_instructions="", is_enabled=True, mcp_server_id=server.id,
                        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(t); db.commit()
    return t


# ---------------------------------------------------------- tools registered


def test_workflow_build_tools_registered():
    import services.builtin.tools.workflow_tools  # noqa: F401 - registers on import
    from services.builtin.internal_tool_registry import internal_tool_registry
    names = set(internal_tool_registry._tools)
    assert {"workflow__generate", "workflow__describe", "workflow__list", "workflow__run"} <= names


def test_workflow_generate_creates_disabled_draft(monkeypatch):
    import services.builtin.tools.workflow_tools as wt

    captured = {}

    async def fake_generate(db, answers):
        captured["answers"] = answers
        return {"id": 7, "name": "Dagelijkse mail", "steps": 3, "_model": "m"}

    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("services.workflows.workflow_ai.generate_and_create", fake_generate)
    monkeypatch.setattr("db.database.SessionLocal", lambda: _FakeSession())

    out = asyncio.run(wt.workflow_generate({"description": "mail me elke dag om 9u een samenvatting"}))
    assert out["status"] == "success"
    assert out["workflow_id"] == 7 and out["steps"] == 3
    assert out["enabled"] is False                      # draft: review before enabling
    assert "review" in out["note"].lower()
    assert captured["answers"]["description"].startswith("mail me")


def test_workflow_generate_requires_description():
    import services.builtin.tools.workflow_tools as wt
    out = asyncio.run(wt.workflow_generate({}))
    assert out["status"] == "error"


def test_workflow_generate_surfaces_backend_error(monkeypatch):
    import services.builtin.tools.workflow_tools as wt

    async def boom(db, answers):
        raise RuntimeError("No model is assigned to generate workflows.")

    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("services.workflows.workflow_ai.generate_and_create", boom)
    monkeypatch.setattr("db.database.SessionLocal", lambda: _FakeSession())
    out = asyncio.run(wt.workflow_generate({"description": "x"}))
    assert out["status"] == "error" and "No model" in out["error"]


# ---------------------------------------------------------- seeded skill


def test_ensure_workflow_building_skill_seeds_and_links(db):
    from db.bootstrap import ensure_workflow_building_skill, _WORKFLOW_BUILDING_INSTRUCTIONS

    srv = _builtin_server(db)
    for n in ("workflow__list", "workflow__run", "workflow__generate", "workflow__describe"):
        _tool(db, srv, n)
    _tool(db, srv, "board_pull")  # unrelated: must NOT be linked

    asyncio.run(ensure_workflow_building_skill(db))

    skill = db.query(skill_model.Skill).filter(skill_model.Skill.name == "workflow_building").one()
    assert skill.is_enabled and skill.source == "builtin"
    assert "workflow__generate" in (skill.instructions or "")
    linked = {db.get(tool_model.Tool, l.tool_id).name
              for l in db.query(st_model.SkillTool).filter(st_model.SkillTool.skill_id == skill.id)}
    assert linked == {"workflow__list", "workflow__run", "workflow__generate", "workflow__describe"}

    # Idempotent + refreshes instructions from code.
    skill.instructions = "stale"
    db.commit()
    asyncio.run(ensure_workflow_building_skill(db))
    db.refresh(skill)
    assert skill.instructions == _WORKFLOW_BUILDING_INSTRUCTIONS
    n_links = db.query(st_model.SkillTool).filter(st_model.SkillTool.skill_id == skill.id).count()
    assert n_links == 4  # no duplicate links


def test_seeded_skill_tools_are_gateway_scoped(db):
    """End-to-end: after seeding, the workflow tools only appear when the skill is
    selected — the context stays lean on unrelated turns."""
    from db.bootstrap import ensure_workflow_building_skill
    from services.mcp import mcp_gateway

    srv = _builtin_server(db)
    for n in ("workflow__list", "workflow__run", "workflow__generate", "workflow__describe"):
        _tool(db, srv, n)
    _tool(db, srv, "board_pull")
    asyncio.run(ensure_workflow_building_skill(db))

    without = {t.name for t in mcp_gateway._list_gateway_tools(db, set())}
    assert "board_pull" in without and "workflow__generate" not in without

    with_skill = {t.name for t in mcp_gateway._list_gateway_tools(db, {"workflow_building"})}
    assert {"workflow__generate", "workflow__describe", "workflow__list", "workflow__run",
            "board_pull"} <= with_skill
