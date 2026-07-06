"""Fresh-install bootstrap (TODO #12). A clean DB (new deploy via the setup wizard)
must get the always-on Builtin MCP server, the system/runtime skill contracts, and a
default agent — idempotently, and WITHOUT clobbering an already-curated DB.
Deterministic; no LLM/network."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from db.bootstrap import (
    ensure_builtin_mcp_server,
    ensure_default_assistant,
    ensure_system_skills,
)
from models.assistant import Assistant
from models.assistant_skill import AssistantSkill
from models.assistant_tool import assistant_tool
from models.mcp_server import MCPServer
from models.skill import Skill
from models.skill_file import SkillFile
from models.skill_tool import SkillTool
from models.tool import Tool


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    # Create the related tables too so SQLAlchemy can configure the Assistant/Skill
    # relationship mappers (Tool, AssistantSkill, …).
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


def test_builtin_server_created_and_idempotent(db):
    asyncio.run(ensure_builtin_mcp_server(db))
    srv = db.query(MCPServer).filter(MCPServer.name == "Builtin").one()
    assert srv.server_type == "builtin"
    assert srv.is_enabled is True
    asyncio.run(ensure_builtin_mcp_server(db))  # idempotent
    assert db.query(MCPServer).filter(MCPServer.name == "Builtin").count() == 1


def test_builtin_server_reenabled_if_disabled(db):
    asyncio.run(ensure_builtin_mcp_server(db))
    srv = db.query(MCPServer).filter(MCPServer.name == "Builtin").one()
    srv.is_enabled = False
    db.commit()
    asyncio.run(ensure_builtin_mcp_server(db))
    db.refresh(srv)
    assert srv.is_enabled is True


def test_system_skills_created(db):
    asyncio.run(ensure_system_skills(db))
    skills = {s.name: s for s in db.query(Skill).all()}
    # the 5 orchestrator_* contracts are system; the runtime_* one is runtime
    assert db.query(Skill).filter(Skill.is_system == True).count() == 5  # noqa: E712
    assert db.query(Skill).filter(Skill.is_runtime == True).count() == 1  # noqa: E712
    assert skills["orchestrator_tool_call_contract"].is_system is True
    assert skills["runtime_file_artifact_inspection"].is_runtime is True
    assert all(s.is_enabled for s in skills.values())
    asyncio.run(ensure_system_skills(db))  # idempotent
    assert db.query(Skill).count() == 6


def test_system_skills_do_not_clobber_existing(db):
    # A pre-existing (curated) row with the same name must be left untouched.
    db.add(Skill(
        name="orchestrator_tool_call_contract", display_name="mine",
        description="custom", instructions="custom",
        is_system=True, is_enabled=False,
    ))
    db.commit()
    asyncio.run(ensure_system_skills(db))
    s = db.query(Skill).filter(Skill.name == "orchestrator_tool_call_contract").one()
    assert s.display_name == "mine"
    assert s.is_enabled is False  # untouched


def test_ensure_roots_creates_data_dirs(tmp_path):
    # Regression for the "[Errno 2] No such file or directory: '/data/db'" — setup on a
    # fresh/empty base dir must create db/ (and the rest) before init_db writes there.
    from component import runtime_paths
    base = tmp_path / "data"  # empty, like a fresh Docker volume
    runtime_paths.ensure_roots(str(base))
    for sub in ("db", "logs", "files", "ask", "voice", ".nd3x"):
        assert (base / sub).is_dir(), f"{sub}/ not created"


def test_default_agent_created_once_and_not_clobbered(db):
    asyncio.run(ensure_default_assistant(db))
    agent = db.query(Assistant).one()
    assert agent.assistant_type == "planner"
    assert agent.is_active is True
    assert agent.schema  # planner schema present
    # Never clobbers an existing agent: rename, re-run, still one and unchanged.
    agent.name = "My Agent"
    db.commit()
    asyncio.run(ensure_default_assistant(db))
    assert db.query(Assistant).count() == 1
    assert db.query(Assistant).one().name == "My Agent"


def test_default_agent_serializes_to_api_schema(db):
    # Regression: a routing_tags=None agent 500'd GET /assistants. The bootstrap agent
    # must validate against the API response schema (routing_tags must be a list).
    from schemas.assistant import AssistantResponse
    asyncio.run(ensure_default_assistant(db))
    agent = db.query(Assistant).one()
    resp = AssistantResponse.model_validate(agent)
    assert resp.routing_tags == []


def test_assistant_response_coerces_null_routing_tags():
    # An older/hand-made agent row with NULL routing_tags must still serialize.
    from datetime import datetime
    from types import SimpleNamespace
    from schemas.assistant import AssistantResponse
    now = datetime(2026, 1, 1)
    row = SimpleNamespace(
        id=1, name="X", description="d", instruction="i", schema={}, assistant_type="planner",
        routing_tags=None, model=None, temperature=None, priority=100,
        is_router_selectable=True, is_active=True,
        created_at=now, updated_at=now, deleted_at=None,
    )
    assert AssistantResponse.model_validate(row).routing_tags == []
