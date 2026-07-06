"""Bulk deletion for Skills: best-effort per id, protected/missing reported as
failed without aborting the batch."""
from __future__ import annotations

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
from models.assistant_tool import assistant_tool
from services.assistants.skill_service import SkillService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            MCPServer.__table__, Assistant.__table__, Skill.__table__, Tool.__table__,
            AssistantSkill.__table__, SkillTool.__table__, SkillFile.__table__, assistant_tool,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _mk(db, name: str, **kw) -> Skill:
    s = Skill(name=name, description="d", instructions="", **kw)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_bulk_delete_removes_normal_skills(db):
    a, b, c = _mk(db, "a"), _mk(db, "b"), _mk(db, "c")
    out = SkillService(db).bulk_delete([a.id, b.id], user=None)
    assert out == {
        "deleted": 2, "failed": 0, "total": 2,
        "results": [{"id": a.id, "status": "deleted"}, {"id": b.id, "status": "deleted"}],
    }
    assert {s.name for s in SkillService(db).get_all()} == {"c"}


def test_bulk_delete_reports_missing_id_without_aborting(db):
    a = _mk(db, "a")
    out = SkillService(db).bulk_delete([a.id, 9999], user=None)
    assert out["deleted"] == 1 and out["failed"] == 1
    statuses = {r["id"]: r["status"] for r in out["results"]}
    assert statuses[a.id] == "deleted" and statuses[9999] == "failed"


def test_bulk_delete_protected_skill_fails_for_non_expert(db):
    normal = _mk(db, "normal")
    system = _mk(db, "sys", is_system=True)
    out = SkillService(db).bulk_delete([normal.id, system.id], user=None)
    assert out["deleted"] == 1 and out["failed"] == 1
    statuses = {r["id"]: r["status"] for r in out["results"]}
    assert statuses[normal.id] == "deleted"
    assert statuses[system.id] == "failed"
    # The protected skill survives.
    assert {s.name for s in SkillService(db).get_all()} == {"sys"}


def test_bulk_delete_empty_list(db):
    assert SkillService(db).bulk_delete([], user=None) == {
        "deleted": 0, "failed": 0, "total": 0, "results": [],
    }
