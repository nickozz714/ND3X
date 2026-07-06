"""Per-model performance metrics rollup (TODO 1.4) — aggregated from the audit
trail: latency percentiles, slow calls, error/timeout rates, validation
failures/recoveries."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.audit import AuditTraceEvent
from services.model_metrics_service import ModelMetricsService


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AuditTraceEvent.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _event(db, type_: str, data: dict, *, level: str = "info", ts: float | None = None):
    db.add(AuditTraceEvent(
        ts=ts if ts is not None else time.time(),
        thread_id="t1", turn_id=1, seq=1,
        type=type_, level=level, summary=type_,
        data_json=json.dumps(data),
    ))
    db.commit()


def test_summarize_rolls_up_per_model(db):
    for dur in (10.0, 20.0, 120.0):
        _event(db, "planner_call_end", {"model": "qwen2.5:14b", "duration_s": dur})
    _event(db, "planner_call_end", {"model": "qwen2.5:14b", "duration_s": 5.0, "empty_output": True})
    _event(db, "planner_call_error", {"model": "qwen2.5:14b", "error": "APITimeoutError", "message": "read timeout"})
    _event(db, "planner_call_error", {"model": "qwen2.5:14b", "error": "RuntimeError", "message": "boom"})
    _event(db, "plan_validation_failed", {"model": "qwen2.5:14b", "retries_used": 0})
    _event(db, "plan_validation_failed", {"model": "qwen2.5:14b", "retries_used": 2})
    _event(db, "planner_call_end", {"model": "gpt-5.5", "duration_s": 3.0})

    out = ModelMetricsService(db).summarize(since_hours=1.0)
    by_model = {m["model"]: m for m in out["models"]}
    q = by_model["qwen2.5:14b"]
    assert q["calls"] == 4
    assert q["slow_calls"] == 1  # 120s >= default 90s threshold
    assert q["empty_outputs"] == 1
    assert q["errors"] == 2
    assert q["timeouts"] == 1
    assert q["error_rate"] == round(2 / 6, 3)
    assert q["validation_failures"] == 2
    assert q["validation_exhausted"] == 1
    assert q["validation_recovered"] == 1
    assert q["avg_s"] is not None and q["max_s"] == 120.0
    assert by_model["gpt-5.5"]["calls"] == 1


def test_summarize_respects_window(db):
    _event(db, "planner_call_end", {"model": "old", "duration_s": 1.0}, ts=time.time() - 7200)
    out = ModelMetricsService(db).summarize(since_hours=1.0)
    assert out["models"] == []


def test_missing_model_grouped_as_slot_routed(db):
    _event(db, "planner_call_end", {"duration_s": 2.0})
    out = ModelMetricsService(db).summarize(since_hours=1.0)
    assert out["models"][0]["model"] == "(slot-routed)"
