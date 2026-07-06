"""Cascade-delete contract for Skills and Tools (TODO §12).

Deleting a Tool or a Skill must remove exactly its association rows in
`skill_tool` / `assistant_skill` / `assistant_tool` and leave zero dangling
links — the data-integrity bug that orphaned assistants pointing at deleted
skills must not recur. All deterministic, no LLM calls.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from db.integrity import find_dangling_links
from models.assistant import Assistant
from models.assistant_skill import AssistantSkill
from models.assistant_tool import assistant_tool
from models.mcp_server import MCPServer
from models.skill import Skill
from models.skill_file import SkillFile
from models.skill_tool import SkillTool
from models.tool import Tool
from repository.skill_repository import SkillRepository
from repository.tool_repository import ToolRepository

_NOW = datetime(2026, 6, 16, 12, 0, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            MCPServer.__table__,
            Assistant.__table__,
            Skill.__table__,
            Tool.__table__,
            AssistantSkill.__table__,
            SkillTool.__table__,
            SkillFile.__table__,
            assistant_tool,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _make_server(db) -> MCPServer:
    server = MCPServer(
        name="srv",
        slug="srv",
        server_type="builtin",
        is_enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def _make_tool(db, server_id: int, name: str) -> Tool:
    tool = Tool(
        remote_name=name,
        name=name,
        description="d",
        argument={},
        type="builtin",
        tool_instructions="",
        is_enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
        mcp_server_id=server_id,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return tool


def _make_skill(db, name: str) -> Skill:
    skill = Skill(name=name, description="d", instructions="")
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def _make_assistant(db, name: str) -> Assistant:
    assistant = Assistant(
        name=name,
        description="d",
        instruction="i",
        schema={},
        created_at=_NOW,
        updated_at=_NOW,
        is_active=True,
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return assistant


def test_delete_tool_used_by_n_skills_removes_exactly_those_links(db):
    server = _make_server(db)
    target = _make_tool(db, server.id, "target")
    other = _make_tool(db, server.id, "other")
    skills = [_make_skill(db, f"s{i}") for i in range(3)]
    assistant = _make_assistant(db, "a")

    # Three skills link the target tool; one skill links the unrelated tool.
    for skill in skills:
        db.add(SkillTool(skill_id=skill.id, tool_id=target.id))
    db.add(SkillTool(skill_id=skills[0].id, tool_id=other.id))
    db.execute(
        assistant_tool.insert().values(assistant_id=assistant.id, tool_id=target.id)
    )
    db.execute(
        assistant_tool.insert().values(assistant_id=assistant.id, tool_id=other.id)
    )
    db.commit()

    assert ToolRepository(db).delete(target.id) is True

    # Exactly the target's links are gone; the unrelated tool's links survive.
    assert db.query(SkillTool).filter(SkillTool.tool_id == target.id).count() == 0
    assert db.query(SkillTool).filter(SkillTool.tool_id == other.id).count() == 1
    remaining_assistant_links = db.execute(assistant_tool.select()).fetchall()
    assert [row.tool_id for row in remaining_assistant_links] == [other.id]
    assert db.query(Tool).filter(Tool.id == target.id).first() is None
    assert find_dangling_links(db) == {}


def test_delete_skill_removes_its_skill_tool_and_assistant_skill_rows(db):
    server = _make_server(db)
    tool = _make_tool(db, server.id, "t")
    target = _make_skill(db, "target")
    other = _make_skill(db, "other")
    assistant = _make_assistant(db, "a")

    db.add(SkillTool(skill_id=target.id, tool_id=tool.id))
    db.add(SkillTool(skill_id=other.id, tool_id=tool.id))
    db.add(AssistantSkill(assistant_id=assistant.id, skill_id=target.id))
    db.add(AssistantSkill(assistant_id=assistant.id, skill_id=other.id))
    db.commit()

    assert SkillRepository(db).delete(target.id) is True

    assert db.query(SkillTool).filter(SkillTool.skill_id == target.id).count() == 0
    assert db.query(AssistantSkill).filter(
        AssistantSkill.skill_id == target.id
    ).count() == 0
    # The unrelated skill keeps both of its links.
    assert db.query(SkillTool).filter(SkillTool.skill_id == other.id).count() == 1
    assert db.query(AssistantSkill).filter(
        AssistantSkill.skill_id == other.id
    ).count() == 1
    assert db.query(Skill).filter(Skill.id == target.id).first() is None
    assert find_dangling_links(db) == {}


def test_find_dangling_links_detects_orphans(db):
    server = _make_server(db)
    tool = _make_tool(db, server.id, "t")
    skill = _make_skill(db, "s")

    # Insert a link, then delete the parent skill out-of-band (raw delete, no
    # cascade) to simulate the historical dangling-row bug.
    db.add(SkillTool(skill_id=skill.id, tool_id=tool.id))
    db.commit()
    db.query(Skill).filter(Skill.id == skill.id).delete(synchronize_session=False)
    db.commit()

    dangling = find_dangling_links(db)
    assert dangling.get("skill_tool.skill_id") == 1
