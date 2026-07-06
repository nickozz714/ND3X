"""Custom slash commands (TODO 4) — CRUD service + name validation."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.slash_command import SlashCommand
from schemas.slash_command import SlashCommandCreate, SlashCommandUpdate
from services.slash_command_service import SlashCommandService


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SlashCommand.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_create_list_update_delete(db):
    svc = SlashCommandService(db)
    c = svc.create(SlashCommandCreate(name="review", description="Review tekst", template="Review dit:\n\n{args}"))
    assert c.id and c.name == "review" and c.is_enabled

    assert [x.name for x in svc.list()] == ["review"]

    svc.update(c.id, SlashCommandUpdate(is_enabled=False))
    assert svc.list(enabled_only=True) == []

    assert svc.delete(c.id) is True
    assert svc.list() == []


def test_name_normalized_and_validated(db):
    svc = SlashCommandService(db)
    c = svc.create(SlashCommandCreate(name="/Samenvatting", template="Vat samen: {args}"))
    assert c.name == "samenvatting"  # leading slash stripped, lowercased

    with pytest.raises(ValueError):
        SlashCommandCreate(name="not valid!", template="x")


def test_duplicate_name_rejected(db):
    svc = SlashCommandService(db)
    svc.create(SlashCommandCreate(name="dup", template="a"))
    with pytest.raises(ValueError):
        svc.create(SlashCommandCreate(name="dup", template="b"))
