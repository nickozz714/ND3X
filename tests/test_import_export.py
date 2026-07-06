"""Export/import for the four config entities. Meeting profiles are the fully
portable case (no cross-entity refs) and cover the envelope + conflict logic."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.meeting_profile as mp
from db.database import Base
from schemas.meeting_profile import MeetingProfileCreate
from services.import_export_service import export, import_envelope, ImportExportError
from services.voice.meeting_profile_service import MeetingProfileService


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[mp.MeetingProfile.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_export_meeting_profiles_envelope(db):
    svc = MeetingProfileService(db)
    svc.create(MeetingProfileCreate(name="Standup", instructions="Keep it short", language="en"))
    env = export(db, "meeting_profile")
    assert env["nd3x_export"] == 1
    assert env["kind"] == "meeting_profile"
    assert len(env["items"]) == 1
    assert env["items"][0]["name"] == "Standup"
    # exported items carry no id / is_default
    assert "id" not in env["items"][0]


def test_export_by_ids(db):
    svc = MeetingProfileService(db)
    a = svc.create(MeetingProfileCreate(name="A"))
    svc.create(MeetingProfileCreate(name="B"))
    env = export(db, "meeting_profile", ids=[a.id])
    assert [i["name"] for i in env["items"]] == ["A"]


def test_import_creates_and_renames_on_conflict(db):
    svc = MeetingProfileService(db)
    svc.create(MeetingProfileCreate(name="Standup"))
    env = {"nd3x_export": 1, "kind": "meeting_profile", "items": [
        {"name": "Standup", "instructions": "x"},   # clashes → renamed
        {"name": "Retro", "instructions": "y"},      # new
    ]}
    out = import_envelope(db, env)
    assert out["created"] == 2
    names = {p.name for p in svc.list()}
    assert "Retro" in names
    assert "Standup (imported)" in names
    assert "Standup" in names  # original untouched


def test_import_roundtrip(db):
    svc = MeetingProfileService(db)
    svc.create(MeetingProfileCreate(name="Sales call", instructions="Track objections",
                                    output_template="# Notes\n", language="nl"))
    env = export(db, "meeting_profile")
    # fresh db
    engine = db.get_bind()
    out = import_envelope(db, env)  # re-import into same db → renamed copy
    assert out["created"] == 1
    imported = next(p for p in svc.list() if p.name == "Sales call (imported)")
    assert imported.instructions == "Track objections"
    assert imported.output_template == "# Notes\n"
    assert imported.language == "nl"


def test_unknown_kind_rejected(db):
    with pytest.raises(ImportExportError):
        export(db, "nonsense")
    with pytest.raises(ImportExportError):
        import_envelope(db, {"kind": "nonsense", "items": []})


def test_import_requires_items(db):
    with pytest.raises(ImportExportError):
        import_envelope(db, {"kind": "meeting_profile"})
