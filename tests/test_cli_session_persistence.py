"""The Claude Code chat-agent session id is persisted per ND3X thread (on the
thread row's metadata) so the next turn resumes instead of re-sending history.
These cover the persistence helpers; all degrade to no-op when the thread row is
missing, so a turn never breaks over session bookkeeping."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Full model registry so every FK/relationship resolves at create_all().
for _m in (
    "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
    "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
    "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
    "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
    "transfer", "meeting_profile", "slash_command", "secret", "board", "workflow",
):
    __import__(f"models.{_m}")

import services.assistants.orchestration.pipeline_runner as pr
from db.database import Base
from models.assistant_thread import AssistantThreadModel


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _thread(db, tid="t1", md=None):
    now = datetime.utcnow().isoformat()
    row = AssistantThreadModel(id=tid, status="active", is_archived=False,
                               metadata_=md or {}, created_at=now, updated_at=now)
    db.add(row)
    db.commit()
    return row


def test_cli_session_roundtrip_read_write_clear(db):
    _thread(db)
    assert pr._read_cli_session(db, "t1") is None
    pr._write_cli_session(db, "t1", "sess-1")
    assert pr._read_cli_session(db, "t1") == "sess-1"
    pr._write_cli_session(db, "t1", "sess-2")  # overwrite (session forked)
    assert pr._read_cli_session(db, "t1") == "sess-2"
    pr._clear_cli_session(db, "t1")            # resume failed → drop it
    assert pr._read_cli_session(db, "t1") is None


def test_cli_session_preserves_other_metadata(db):
    _thread(db, md={"keep": "me"})
    pr._write_cli_session(db, "t1", "sess-1")
    db.expire_all()
    row = db.get(AssistantThreadModel, "t1")
    assert row.metadata_["keep"] == "me" and row.metadata_["cli_session_id"] == "sess-1"


def test_cli_session_missing_thread_is_safe(db):
    assert pr._read_cli_session(db, "nope") is None
    pr._write_cli_session(db, "nope", "x")  # no row → silently ignored
    assert pr._read_cli_session(db, "nope") is None
    pr._clear_cli_session(db, "nope")       # no row → no error
    assert pr._read_cli_session(db, None) is None  # no thread id → None
