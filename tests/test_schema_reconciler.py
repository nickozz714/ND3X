"""Add-only schema reconciliation: an older DB gains missing model columns on
startup, without losing existing data."""
from __future__ import annotations

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from db.schema_reconciler import reconcile_schema


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _new_schema() -> tuple[MetaData, Table]:
    md = MetaData()
    t = Table(
        "widget", md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
        Column("color", String(20), nullable=True),                 # added, nullable
        Column("active", Boolean, nullable=False, default=True),     # added, NOT NULL + default
        Column("notes", String(200), nullable=False),                # added, NOT NULL, no default
    )
    return md, t


def _create_old_widget(db):
    # The "old" table predates color/active/notes.
    db.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL)"))
    db.execute(text("INSERT INTO widget (id, name) VALUES (1, 'existing')"))
    db.commit()


def test_reconcile_adds_missing_columns_and_keeps_data(db):
    _create_old_widget(db)
    md, _ = _new_schema()

    applied = reconcile_schema(db, metadata=md)

    added_cols = {c["name"] for c in inspect(db.bind).get_columns("widget")}
    assert {"color", "active", "notes"}.issubset(added_cols)
    assert len(applied) == 3
    # Existing row survived untouched.
    row = db.execute(text("SELECT id, name FROM widget WHERE id = 1")).fetchone()
    assert row == (1, "existing")


def test_reconcile_is_idempotent(db):
    _create_old_widget(db)
    md, _ = _new_schema()
    reconcile_schema(db, metadata=md)
    # Second pass finds nothing to add.
    assert reconcile_schema(db, metadata=md) == []


def test_reconcile_skips_missing_tables(db):
    # Table does not exist yet → create_all() owns it, reconciler leaves it alone.
    md, _ = _new_schema()
    assert reconcile_schema(db, metadata=md) == []
    assert "widget" not in set(inspect(db.bind).get_table_names())


def test_not_null_without_default_added_as_nullable(db):
    _create_old_widget(db)
    md, _ = _new_schema()
    reconcile_schema(db, metadata=md)
    # 'notes' is NOT NULL in the model but had no default → added nullable so the
    # ALTER does not fail on the existing row.
    notes = next(c for c in inspect(db.bind).get_columns("widget") if c["name"] == "notes")
    assert notes["nullable"] is True
