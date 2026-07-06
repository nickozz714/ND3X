"""Tests for the DB-backed settings registry (services/app_settings_registry)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.application_settings as app_models
from component.config import settings
from services import app_settings_registry as reg


@pytest.fixture(autouse=True)
def _restore_settings():
    """hydrate() mutates the global settings snapshot for every registered key by
    design; snapshot + restore so these tests don't leak into others."""
    saved = {s.key: getattr(settings, s.key, None) for s in reg.SPEC}
    saved["BASE_DIR"] = getattr(settings, "BASE_DIR", "")
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    app_models.ApplicationSetting.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_seed_all_creates_rows(db):
    reg.seed_all(db)
    rows = {r.key: r.value for r in db.query(app_models.ApplicationSetting).all()}
    assert len(rows) == len(reg.SPEC)
    assert "MAX_TOOL_STEPS" in rows


def test_apply_updates_mutates_live_settings(db):
    reg.seed_all(db)
    original = settings.MAX_TOOL_STEPS
    try:
        reg.apply_updates(db, {"MAX_TOOL_STEPS": "99"})
        assert settings.MAX_TOOL_STEPS == 99  # hydrated into the live snapshot
    finally:
        settings.MAX_TOOL_STEPS = original


def test_env_override_beats_db(db, monkeypatch):
    reg.seed_all(db)
    original = settings.MAX_TOOL_STEPS
    try:
        # DB says 99, but an env override of the same name must win (hydrate skips it).
        row = next(r for r in db.query(app_models.ApplicationSetting).all() if r.key == "MAX_TOOL_STEPS")
        row.value = "99"
        db.commit()
        monkeypatch.setenv("MAX_TOOL_STEPS", "7")
        settings.MAX_TOOL_STEPS = original  # simulate import-time value
        reg.hydrate(db)
        assert settings.MAX_TOOL_STEPS != 99  # env override kept hydrate from applying the DB value
    finally:
        settings.MAX_TOOL_STEPS = original


def test_apply_updates_ignores_unknown_keys(db):
    reg.seed_all(db)
    written = reg.apply_updates(db, {"NOT_A_REAL_SETTING": "x"})
    assert written == 0


def test_path_settings_are_base_relative(db, monkeypatch):
    monkeypatch.setattr(settings, "BASE_DIR", "/data/nd3x", raising=False)
    # rel_to_base relativizes absolutes under base and strips leading ./
    assert reg.rel_to_base("/data/nd3x/files") == "files"
    assert reg.rel_to_base("./files") == "files"
    assert reg.rel_to_base("ask") == "ask"
    # abs_under_base resolves a sub-path to an absolute under base
    assert reg.abs_under_base("files") == "/data/nd3x/files"

    reg.seed_all(db)
    original = settings.FILES_DIR
    try:
        reg.apply_updates(db, {"FILES_DIR": "/data/nd3x/myfiles"})
        # stored relative, live config resolved absolute under base
        row = next(r for r in db.query(app_models.ApplicationSetting).all() if r.key == "FILES_DIR")
        assert row.value == "myfiles"
        assert settings.FILES_DIR == "/data/nd3x/myfiles"
    finally:
        settings.FILES_DIR = original


def test_mcp_not_in_registry(db):
    assert not any(s.key.startswith("MCP_") for s in reg.SPEC)


def test_research_provider_present(db):
    keys = {s.key for s in reg.SPEC}
    assert "RESEARCH_PROVIDER" in keys


def test_groups_masks_secrets(db):
    reg.seed_all(db)
    db_row = next(r for r in db.query(app_models.ApplicationSetting).all() if r.key == "EXA_API_KEY")
    db_row.value = "super-secret"
    db.commit()
    groups = reg.groups(db)
    flat = {s["key"]: s for g in groups for s in g["settings"]}
    assert flat["EXA_API_KEY"]["secret"] is True
    assert flat["EXA_API_KEY"]["value"] == ""  # masked
    assert flat["EXA_API_KEY"]["has_value"] is True
