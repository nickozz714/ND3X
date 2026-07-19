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
    assert {"workflow__create", "workflow__describe", "workflow__list", "workflow__run"} <= names


def test_workflow_create_persists_agent_design(db, monkeypatch):
    """The AGENT designs the steps (it has the context); workflow__create persists
    them as a DISABLED linear draft — no second model involved."""
    import services.builtin.tools.workflow_tools as wt
    import models.workflow  # noqa: F401
    import models.assistant  # noqa: F401

    monkeypatch.setattr("db.database.SessionLocal", lambda: _NonClosing(db))
    monkeypatch.setattr("services.workflows.workflow_ai._resolve_agent_id", lambda _db: 42)

    out = asyncio.run(wt.workflow_create({
        "name": "Ochtend-samenvatting",
        "description": "Elke ochtend een samenvatting mailen",
        "operations": [
            {"type": "assistant", "name": "Schrijf samenvatting",
             "question": "Vat het nieuws samen"},
            {"type": "notification", "name": "Mail Nick", "channel": "email",
             "subject": "Ochtend", "message": "{{answer}}",
             "recipients": ["nick@example.com"]},
        ],
    }))
    assert out["status"] == "success" and out["steps"] == 2
    assert out["enabled"] is False                      # draft: review before enabling

    from services.workflows.workflow_service import WorkflowService
    wf = WorkflowService(db).get_by_id(out["workflow_id"])
    assert wf.is_enabled is False
    ops = sorted(wf.operations, key=lambda o: o.position)
    assert [o.operation_type for o in ops] == ["assistant", "notification"]
    assert ops[0].operation_ref_id == 42                # agent step bound to the agent
    assert ops[1].config["channel"] == "email"
    assert ops[1].config["recipients"] == ["nick@example.com"]
    assert ops[1].depends_on == [ops[0].id]             # linear chain (service maps position → id)


def test_workflow_create_validates_input(monkeypatch):
    import services.builtin.tools.workflow_tools as wt
    assert asyncio.run(wt.workflow_create({}))["status"] == "error"
    assert asyncio.run(wt.workflow_create({"name": "x"}))["status"] == "error"
    out = asyncio.run(wt.workflow_create(
        {"name": "x", "operations": [{"type": "condition"}]}))
    assert out["status"] == "error" and "unknown type" in out["error"]


class _NonClosing:
    """Context manager handing out an existing session without closing it."""
    def __init__(self, db): self._db = db
    def __enter__(self): return self._db
    def __exit__(self, *a): return False


# ---------------------------------------------------------- seeded skill


def test_ensure_workflow_building_skill_seeds_and_links(db):
    from db.bootstrap import ensure_workflow_building_skill, _WORKFLOW_BUILDING_INSTRUCTIONS

    srv = _builtin_server(db)
    for n in ("workflow__list", "workflow__run", "workflow__create", "workflow__describe"):
        _tool(db, srv, n)
    _tool(db, srv, "board_pull")  # unrelated: must NOT be linked

    asyncio.run(ensure_workflow_building_skill(db))

    skill = db.query(skill_model.Skill).filter(skill_model.Skill.name == "workflow_building").one()
    assert skill.is_enabled and skill.source == "builtin"
    assert "workflow__create" in (skill.instructions or "")
    linked = {db.get(tool_model.Tool, l.tool_id).name
              for l in db.query(st_model.SkillTool).filter(st_model.SkillTool.skill_id == skill.id)}
    assert linked == {"workflow__list", "workflow__run", "workflow__create", "workflow__describe"}

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
    for n in ("workflow__list", "workflow__run", "workflow__create", "workflow__describe"):
        _tool(db, srv, n)
    _tool(db, srv, "board_pull")
    asyncio.run(ensure_workflow_building_skill(db))

    without = {t.name for t in mcp_gateway._list_gateway_tools(db, set())}
    assert "board_pull" in without and "workflow__create" not in without

    with_skill = {t.name for t in mcp_gateway._list_gateway_tools(db, {"workflow_building"})}
    assert {"workflow__create", "workflow__describe", "workflow__list", "workflow__run",
            "board_pull"} <= with_skill
