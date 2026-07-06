"""Workflow http_request secret injection + masking (the AI-never-sees-it path)."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.secret as secret_model
import utils.crypto as crypto
from component.config import settings
from db.database import Base
from schemas.secret import SecretCreate
from services.secret_service import SecretService
from services.workflows.workflow_executor import WorkflowExecutor

# Importing the executor pulls in models with string relationships (MCPServer,
# Tool, Skill, …). Register the whole graph so SQLAlchemy can configure mappers
# on commit, even though this test only creates a Secret table.
import models.mcp_server  # noqa: F401,E402
import models.tool  # noqa: F401,E402
import models.assistant  # noqa: F401,E402
import models.assistant_tool  # noqa: F401,E402
import models.skill  # noqa: F401,E402
import models.skill_tool  # noqa: F401,E402
import models.assistant_skill  # noqa: F401,E402


class _Repo:
    def __init__(self, db):
        self.db = db


@pytest.fixture()
def executor(monkeypatch):
    monkeypatch.setattr(settings, "MAIL_SECRET_KEY", Fernet.generate_key().decode(), raising=False)
    crypto._fernet.cache_clear()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[secret_model.Secret.__table__])
    db = sessionmaker(bind=engine)()
    SecretService(db).create(SecretCreate(name="token", value="sk-live-XYZ"))
    ex = WorkflowExecutor.__new__(WorkflowExecutor)  # skip heavy __init__
    ex.workflow_repository = _Repo(db)
    try:
        yield ex
    finally:
        db.close()
        crypto._fernet.cache_clear()


def test_inject_secrets_resolves_and_collects(executor):
    collected: list[str] = []
    out = executor._inject_secrets({"Authorization": "Bearer ${secret.token}"}, collected)
    assert out == {"Authorization": "Bearer sk-live-XYZ"}
    assert collected == ["sk-live-XYZ"]


def test_inject_secrets_unknown_raises(executor):
    with pytest.raises(ValueError, match="Unknown secret"):
        executor._inject_secrets("${secret.missing}", [])


def test_mask_secrets_redacts_plaintext(executor):
    collected: list[str] = []
    executor._inject_secrets("Bearer ${secret.token}", collected)
    masked = executor._mask_secrets("log line with sk-live-XYZ inside", collected)
    assert "sk-live-XYZ" not in masked
    assert "[secret]" in masked


def test_template_resolution_preserves_secret_then_injects(executor):
    # http_request resolves templates first (must keep ${secret.X} intact) then
    # injects at the boundary — the earlier bug blanked it to "" here.
    tmpl = executor._resolve_template_value("Bearer ${secret.token}", {}, allow_null=True)
    assert tmpl == "Bearer ${secret.token}"
    collected: list[str] = []
    assert executor._inject_secrets(tmpl, collected) == "Bearer sk-live-XYZ"


def test_secret_literal_stays_inert_outside_http(executor):
    # Elsewhere (set_variable, assistant input) the placeholder is never injected,
    # so the AI only ever sees the inert ${secret.X}, never the value.
    out = executor._resolve_template_value({"note": "key=${secret.token}"}, {}, allow_null=True)
    assert out == {"note": "key=${secret.token}"}
    assert "sk-live-XYZ" not in str(out)


def test_non_secret_values_pass_through(executor):
    collected: list[str] = []
    out = executor._inject_secrets({"X-Static": "plain", "n": 5}, collected)
    assert out == {"X-Static": "plain", "n": 5}
    assert collected == []
