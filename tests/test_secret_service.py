"""Native encrypted secret store: encryption roundtrip, .env import, placeholder
resolution + masking."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.secret as secret_model
import utils.crypto as crypto
from component.config import settings
from db.database import Base
from schemas.secret import SecretCreate, SecretUpdate
from services.secret_service import SecretError, SecretService


@pytest.fixture()
def db(monkeypatch):
    # Encryption needs a Fernet key; provider secrets already require this.
    monkeypatch.setattr(settings, "MAIL_SECRET_KEY", Fernet.generate_key().decode(), raising=False)
    crypto._fernet.cache_clear()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[secret_model.Secret.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        crypto._fernet.cache_clear()


def test_create_encrypts_and_never_stores_plaintext(db):
    svc = SecretService(db)
    row = svc.create(SecretCreate(name="api_key", value="sk-live-secret"))
    assert row.value_encrypted and "sk-live-secret" not in row.value_encrypted
    assert svc.get_value("api_key") == "sk-live-secret"


def test_create_without_value_is_placeholder(db):
    svc = SecretService(db)
    row = svc.create(SecretCreate(name="pending"))
    assert row.value_encrypted is None
    assert row.placeholder is True
    assert svc.get_value("pending") is None


def test_duplicate_name_rejected(db):
    svc = SecretService(db)
    svc.create(SecretCreate(name="dup", value="x"))
    with pytest.raises(SecretError):
        svc.create(SecretCreate(name="dup", value="y"))


def test_invalid_name_rejected(db):
    with pytest.raises(SecretError):
        SecretService(db).create(SecretCreate(name="bad name!", value="x"))


def test_update_value_and_clear(db):
    svc = SecretService(db)
    svc.create(SecretCreate(name="k", value="one"))
    svc.update("k", SecretUpdate(value="two"))
    assert svc.get_value("k") == "two"
    svc.update("k", SecretUpdate(value=""))  # empty clears
    assert svc.get_value("k") is None


def test_obfuscate_masks_middle():
    assert SecretService._obfuscate("sk-live-abcdef")[:2] == "sk"
    assert SecretService._obfuscate("sk-live-abcdef").endswith("ef")
    assert "•" in SecretService._obfuscate("sk-live-abcdef")
    assert SecretService._obfuscate("ab") == "••"
    assert SecretService._obfuscate(None) == ""


def test_parse_env_handles_comments_export_and_quotes():
    parsed = dict(SecretService._parse_env(
        "# a comment\n"
        "export FOO=bar\n"
        "BAZ=\"quoted value\"\n"
        "QUX='single'\n"
        "\n"
        "no_equals_line\n"
        "bad key=skipme\n"
    ))
    assert parsed == {"FOO": "bar", "BAZ": "quoted value", "QUX": "single"}


def test_import_env_create_skip_overwrite(db):
    svc = SecretService(db)
    res = svc.import_env("A=1\nB=2\n")
    assert res["created"] == 2 and res["total"] == 2
    # Re-import without overwrite skips existing.
    res2 = svc.import_env("A=9\nC=3\n")
    assert res2["created"] == 1 and res2["skipped"] == 1
    assert svc.get_value("A") == "1"
    # With overwrite it updates.
    res3 = svc.import_env("A=9\n", overwrite=True)
    assert res3["updated"] == 1
    assert svc.get_value("A") == "9"


def test_resolve_placeholders_and_collect_values(db):
    svc = SecretService(db)
    svc.create(SecretCreate(name="token", value="XYZ"))
    resolved, values, unresolved = svc.resolve_placeholders("Bearer ${secret.token}")
    assert resolved == "Bearer XYZ"
    assert values == ["XYZ"]
    assert unresolved == []


def test_resolve_placeholders_reports_unknown(db):
    resolved, values, unresolved = SecretService(db).resolve_placeholders("${secret.nope}")
    assert unresolved == ["nope"]
    assert resolved == "${secret.nope}"  # left intact so callers can error clearly


def test_has_placeholder():
    assert SecretService.has_placeholder("x ${secrets.a} y")
    assert SecretService.has_placeholder("${secret.a}")
    assert not SecretService.has_placeholder("no placeholder here")
