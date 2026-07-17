"""Tests for the agent Kanban board: service selection/ordering, the board__*
tools, and the board_pull workflow selection primitive."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.board as bm
# Register the FULL model class registry so SQLAlchemy can resolve every
# relationship string when WorkflowExecutor is imported (a partial import
# leaves configure_mappers unable to resolve names like 'MCPServer' on first
# query in isolation). No tables are created for these — import is enough.
for _m in (
    "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
    "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
    "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
    "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
    "transfer", "meeting_profile", "slash_command", "secret", "workflow",
):
    __import__(f"models.{_m}")
from schemas.board import BoardItemCreate, BoardItemUpdate
from services.board_service import BoardService


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    bm.BoardItem.__table__.create(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _mk(svc, title, **kw):
    return svc.create_item(BoardItemCreate(title=title, **kw), actor=kw.get("origin", "agent"))


# ------------------------------------------------------------------- service


def test_list_orders_by_priority_then_position(db):
    svc = BoardService(db)
    _mk(svc, "low", priority="low")
    _mk(svc, "urgent", priority="urgent")
    _mk(svc, "medium", priority="medium")
    titles = [i.title for i in svc.list_items(status="todo")]
    assert titles == ["urgent", "medium", "low"]


def test_pull_top_n_of_column(db):
    svc = BoardService(db)
    for i in range(5):
        _mk(svc, f"t{i}", priority="high")
    _mk(svc, "doing-one", priority="urgent", status="doing")
    picked = svc.pull(status="todo", limit=3)
    assert len(picked) == 3
    assert all(p.status == "todo" for p in picked)


def test_ready_only_skips_unfinished_dependencies(db):
    svc = BoardService(db)
    dep = _mk(svc, "dependency")
    blocked = _mk(svc, "needs dep", depends_on=[dep.id])
    # Not ready: dependency still todo.
    assert svc.is_ready(blocked) is False
    assert [i.id for i in svc.pull(status="todo", ready_only=True)] == [dep.id]
    # Finish the dependency → the dependent becomes ready.
    svc.move_item(dep.id, "done", actor="agent")
    ready_ids = [i.id for i in svc.list_items(status="todo", ready_only=True)]
    assert blocked.id in ready_ids


def test_update_sets_result_and_status(db):
    svc = BoardService(db)
    it = _mk(svc, "task")
    svc.update_item(it.id, BoardItemUpdate(status="done", result="shipped"), actor="agent")
    fresh = svc.get_item(it.id)
    assert fresh.status == "done" and fresh.result == "shipped" and fresh.updated_by == "agent"


def test_invalid_enum_rejected(db):
    svc = BoardService(db)
    with pytest.raises(ValueError, match="status must be one of"):
        _mk(svc, "x", status="in_progress")  # not a valid column


# --------------------------------------------------------------------- tools


def _patch_session(monkeypatch, db):
    # board__* tools open their own SessionLocal; point it at the test session.
    import services.builtin.tools.board_tools as bt

    class _CtxSession:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bt, "SessionLocal", lambda: _CtxSession(), raising=False)
    # SessionLocal is imported inside each tool fn from db.database — patch there.
    import db.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _CtxSession())


def test_tool_create_list_move(monkeypatch, db):
    import services.builtin.tools.board_tools as bt
    _patch_session(monkeypatch, db)

    created = asyncio.run(bt.board_create({"title": "agent task", "priority": "high",
                                          "acceptance": "tests pass"}))
    assert created["status"] == "success"
    item_id = created["item"]["id"]
    assert created["item"]["origin"] == "agent"

    listed = asyncio.run(bt.board_list({"status": "todo"}))
    assert listed["count"] == 1 and listed["items"][0]["id"] == item_id

    moved = asyncio.run(bt.board_move({"id": item_id, "status": "done", "result": "ok"}))
    assert moved["item"]["status"] == "done" and moved["item"]["result"] == "ok"

    # Empty title rejected.
    bad = asyncio.run(bt.board_create({"title": "  "}))
    assert bad["status"] == "error"


def test_tool_get_missing(monkeypatch, db):
    import services.builtin.tools.board_tools as bt
    _patch_session(monkeypatch, db)
    out = asyncio.run(bt.board_get({"id": 999}))
    assert out["status"] == "error"


# --------------------------------------------------------------- board_pull


def test_board_pull_emits_iterable_and_claims(monkeypatch, db):
    # Drive _execute_board_pull_operation without a full executor: bind the
    # method and point its SessionLocal at the test db.
    import db.database as dbmod
    from services.workflows.workflow_executor import WorkflowExecutor

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _Ctx())

    svc = BoardService(db)
    for i in range(4):
        _mk(svc, f"item{i}", priority="high")

    class _Op:
        id = 1
        operation_type = "board_pull"
        config = {"status": "todo", "limit": 2, "iterable_name": "items", "claim": True}

    ex = WorkflowExecutor.__new__(WorkflowExecutor)
    out = asyncio.run(WorkflowExecutor._execute_board_pull_operation(ex, _Op(), {}, {}))
    assert out["status"] == "success" and out["picked_count"] == 2
    iterable = out["downstream_handoff"]["iterables"]["items"]
    assert len(iterable) == 2
    assert all("board_item_id" in it and "title" in it for it in iterable)
    # Claimed → moved to doing, so a second pull of 'todo' returns the rest.
    assert len(svc.pull(status="todo", limit=10)) == 2
    assert len(svc.list_items(status="doing")) == 2
