"""Graceful degradation: a skill whose tools are missing (deleted) or disabled
stays USABLE — its dead/disabled tools are silently omitted, the skill still
appears in the selectable catalog, and nothing blocks selecting it. Guards the
"don't block the whole skill, just ignore the missing tools" contract.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.assistant import Assistant
from models.assistant_skill import AssistantSkill
from models.mcp_server import MCPServer
from models.skill import Skill
from models.skill_file import SkillFile
from models.skill_tool import SkillTool
from models.tool import Tool
from repository.skill_tool_repository import SkillToolRepository
from services.assistants.prompt_builder import PromptBuilder

_NOW = datetime(2026, 7, 17, 12, 0, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        MCPServer.__table__, Assistant.__table__, Skill.__table__, Tool.__table__,
        AssistantSkill.__table__, SkillTool.__table__, SkillFile.__table__,
    ])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _server(db):
    s = MCPServer(name="srv", slug="srv", server_type="builtin", is_enabled=True,
                  created_at=_NOW, updated_at=_NOW)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _tool(db, server_id, name, *, enabled=True):
    t = Tool(remote_name=name, name=name, description="d", argument={}, type="builtin",
             tool_instructions="", is_enabled=enabled, created_at=_NOW, updated_at=_NOW,
             mcp_server_id=server_id)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _skill(db, name):
    s = Skill(name=name, description="does X", instructions="")
    db.add(s); db.commit(); db.refresh(s)
    return s


def test_runtime_loads_only_live_enabled_tools(db):
    server = _server(db)
    good = _tool(db, server.id, "good")
    disabled = _tool(db, server.id, "disabled_tool", enabled=False)
    gone = _tool(db, server.id, "gone")
    skill = _skill(db, "mixed")
    db.add_all([
        SkillTool(skill_id=skill.id, tool_id=good.id),
        SkillTool(skill_id=skill.id, tool_id=disabled.id),
        SkillTool(skill_id=skill.id, tool_id=gone.id),
    ])
    db.commit()
    # Simulate a tool that no longer exists: raw-delete the Tool row, leaving a
    # dangling skill_tool link (the historical "missing tool" case).
    db.query(Tool).filter(Tool.id == gone.id).delete(synchronize_session=False)
    db.commit()

    repo = SkillToolRepository(db)

    # Runtime load (enabled_only) → only the live, enabled tool; missing + disabled
    # are silently omitted, no crash. The skill remains loadable.
    runtime = sorted(t.name for _, t in repo.get_for_skill(skill.id, enabled_only=True))
    assert runtime == ["good"]

    # Even without the enabled filter, a MISSING tool is dropped by the inner join
    # (never a dangling reference); a disabled-but-existing tool is still listed.
    everything = sorted(t.name for _, t in repo.get_for_skill(skill.id, enabled_only=False))
    assert everything == ["disabled_tool", "good"]  # "gone" cannot resurface


def test_toolless_skill_still_selectable_in_catalog():
    """A skill with zero live tools (all missing/disabled, or intentionally
    instruction-only) is NOT hidden or blocked — it stays in the selectable
    catalog the agent chooses from."""
    pb = PromptBuilder()
    assistant = SimpleNamespace(skills=[
        SimpleNamespace(name="mixed", description="does X", is_enabled=True,
                        is_system=False, is_runtime=False, tools=[]),
        SimpleNamespace(name="has_tool", description="does Y", is_enabled=True,
                        is_system=False, is_runtime=False,
                        tools=[SimpleNamespace(id=1, name="good", is_enabled=True)]),
    ])
    catalog = pb.render_skill_catalog(assistant)
    # both are offered; a toolless skill is not filtered out
    assert "mixed: does X" in catalog
    assert "has_tool: does Y" in catalog
