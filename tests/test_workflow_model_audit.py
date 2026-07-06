"""Detect workflow assistant ops whose pinned model override is no longer a
registered/enabled chat model."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.workflow import Workflow, WorkflowOperation
from services.workflows import workflow_model_audit


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Workflow.__table__, WorkflowOperation.__table__])
    session = sessionmaker(bind=engine)()

    # Stub the registry: only gpt-5.4-mini is a registered+enabled chat model.
    class _Reg:
        def __init__(self, _db):
            pass

        def list_models(self, *, capability=None):
            return [
                SimpleNamespace(model_id="gpt-5.4-mini", enabled=True),
                SimpleNamespace(model_id="qwen2.5:14b", enabled=True),
                SimpleNamespace(model_id="gpt-old-disabled", enabled=False),
            ]

    monkeypatch.setattr(
        "services.providers.registry_service.ProviderRegistryService", _Reg
    )
    try:
        yield session
    finally:
        session.close()


def _wf(db, name: str, deleted=False) -> Workflow:
    wf = Workflow(name=name, input_schema={}, is_enabled=True)
    if deleted:
        from datetime import datetime
        wf.deleted_at = datetime.utcnow()
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return wf


def _op(db, wf_id, name, model, position=1):
    op = WorkflowOperation(
        workflow_id=wf_id, name=name, operation_type="assistant",
        operation_ref_id=1, config=({"model": model} if model is not None else {}),
        position=position,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def test_flags_stale_override(db):
    wf = _wf(db, "News")
    _op(db, wf.id, "valid", "gpt-5.4-mini", 1)
    _op(db, wf.id, "stale", "gpt-5-mini", 2)      # renamed → not registered
    _op(db, wf.id, "no-override", None, 3)
    _op(db, wf.id, "disabled", "gpt-old-disabled", 4)  # exists but disabled

    issues = workflow_model_audit.find_stale_model_overrides(db)
    flagged = {i["operation_name"]: i["pinned_model"] for i in issues}
    assert flagged == {"stale": "gpt-5-mini", "disabled": "gpt-old-disabled"}
    assert issues[0]["workflow_name"] == "News"


def test_ignores_soft_deleted_workflows(db):
    wf = _wf(db, "Gone", deleted=True)
    _op(db, wf.id, "stale", "gpt-5-mini", 1)
    assert workflow_model_audit.find_stale_model_overrides(db) == []


def test_log_returns_count(db):
    wf = _wf(db, "News")
    _op(db, wf.id, "stale", "gpt-5-mini", 1)
    assert workflow_model_audit.log_stale_model_overrides(db) == 1
