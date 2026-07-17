"""Tests for the persisted background-task registry: DB mirror, boot restore
(orphaned running → error + unacknowledged), and the panel service."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.background_task import BackgroundTask
from services.background_task_service import BackgroundTaskService
from services.builtin.tools import background_tasks as bt


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[BackgroundTask.__table__])
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def persisted(db, monkeypatch):
    """Route the best-effort persistence layer to the in-memory test DB."""
    monkeypatch.setattr(bt, "_open_session", lambda: db)
    monkeypatch.setattr(bt.settings, "CONFIGURED", True, raising=False)
    # _persist_sync closes the session; keep the fixture session alive instead.
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(bt, "_TASKS", {})
    return db


def test_persist_sync_upserts(persisted):
    rec = {
        "id": "bg-abc123", "status": "running", "owner_thread": "t1",
        "assistant": "ad-hoc", "task_preview": "doe iets", "created_at": 1,
        "finished_at": None, "result": None, "_acknowledged": False,
    }
    bt._persist_sync(rec)
    row = persisted.get(BackgroundTask, "bg-abc123")
    assert row is not None and row.status == "running" and row.acknowledged is False

    rec.update(status="done", finished_at=2, result={"summary": "klaar"}, _acknowledged=True)
    bt._persist_sync(rec)
    persisted.expire_all()
    row = persisted.get(BackgroundTask, "bg-abc123")
    assert row.status == "done" and row.result == {"summary": "klaar"} and row.acknowledged is True


def test_persist_sync_noop_when_unconfigured(db, monkeypatch):
    monkeypatch.setattr(bt, "_open_session", lambda: db)
    monkeypatch.setattr(bt.settings, "CONFIGURED", False, raising=False)
    bt._persist_sync({"id": "bg-skip", "status": "running"})
    assert db.get(BackgroundTask, "bg-skip") is None


def test_restore_marks_interrupted_running_as_error(persisted):
    persisted.add(BackgroundTask(id="bg-run1", status="running", owner_thread="t1",
                                 assistant="ad-hoc", task_preview="lang werk", created_at=1))
    persisted.add(BackgroundTask(id="bg-done1", status="done", owner_thread="t1",
                                 created_at=2, finished_at=3, result={"summary": "ok"}, acknowledged=True))
    persisted.commit()

    restored = bt.restore_persisted_tasks(persisted)
    assert restored == 2

    rec = bt._TASKS["bg-run1"]
    assert rec["status"] == "error"
    assert rec["_acknowledged"] is False
    assert "herstart" in rec["result"]["error"]
    assert rec["finished_at"] is not None
    row = persisted.get(BackgroundTask, "bg-run1")
    assert row.status == "error" and row.acknowledged is False

    # de al-bevestigde done-taak blijft bevestigd (geen dubbele notificatie)
    assert bt._TASKS["bg-done1"]["_acknowledged"] is True

    # de onderbroken taak wordt in de volgende turn van de eigenaar gedraind
    drained = asyncio.run(bt.drain_completed_background_tasks("t1"))
    assert [d["task_id"] for d in drained] == ["bg-run1"]
    assert asyncio.run(bt.drain_completed_background_tasks("t1")) == []


def test_service_list_filter_and_delete(db):
    for i, thread in enumerate(["t1", "t1", "t2"]):
        db.add(BackgroundTask(id=f"bg-{i}", status="done", owner_thread=thread, created_at=i))
    db.commit()

    svc = BackgroundTaskService(db)
    assert [t.id for t in svc.list()] == ["bg-2", "bg-1", "bg-0"]  # nieuwste eerst
    assert [t.id for t in svc.list(thread_id="t1")] == ["bg-1", "bg-0"]
    assert svc.delete("bg-2") is True
    assert svc.delete("bg-2") is False
    assert len(svc.list()) == 2
