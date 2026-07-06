"""UsageService: token ledger, per-conversation context budget, global summary,
and the monthly budget — all deterministic (no LLM calls)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.token_usage import TokenUsage, UsageBudget
from services.usage_service import UsageService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[TokenUsage.__table__, UsageBudget.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_thread_usage_context_vs_cost(db):
    svc = UsageService(db)
    # Two stage calls in a turn: the LATEST input (200) is the current context size;
    # used_tokens is the cumulative cost (650), not the context occupancy.
    svc.record(thread_id="t1", input_tokens=300, output_tokens=100, model="gpt-5.5-pro", provider_type="openai")
    svc.record(thread_id="t1", input_tokens=200, output_tokens=50, model="gpt-5.5-pro", provider_type="openai")
    u = svc.thread_usage("t1", context_window=2000)
    assert u["input_tokens"] == 500
    assert u["output_tokens"] == 150
    assert u["used_tokens"] == 650            # cumulative cost
    assert u["context_tokens"] == 200         # latest input = current context
    assert u["context_window"] == 2000
    assert u["tokens_left"] == 1800           # window − context_tokens
    assert u["ratio"] == pytest.approx(0.10)
    assert u["near_limit"] is False


def test_near_limit_flag(db):
    svc = UsageService(db)
    # latest input is the context occupancy → 850/1000 = 0.85 ≥ threshold
    svc.record(thread_id="t2", input_tokens=850, output_tokens=100, model="m")
    u = svc.thread_usage("t2", context_window=1000)
    assert u["context_tokens"] == 850
    assert u["near_limit"] is True


def test_thread_usage_without_window(db):
    svc = UsageService(db)
    svc.record(thread_id="t3", input_tokens=10, output_tokens=5)
    u = svc.thread_usage("t3")
    assert u["used_tokens"] == 15
    assert u["context_tokens"] == 10
    assert u["context_window"] is None
    assert u["tokens_left"] is None
    assert u["near_limit"] is False


def test_summary_grouping(db):
    svc = UsageService(db)
    svc.record(thread_id="a", input_tokens=100, output_tokens=0, model="gpt-5.5", provider_type="openai")
    svc.record(thread_id="b", input_tokens=200, output_tokens=0, model="qwen2.5:14b", provider_type="ollama")
    svc.record(thread_id="c", input_tokens=50, output_tokens=0, model="gpt-5.5", provider_type="openai")
    s = svc.summary()
    assert s["total_tokens"] == 350
    by_model = {r["key"]: r["total_tokens"] for r in s["by_model"]}
    assert by_model == {"gpt-5.5": 150, "qwen2.5:14b": 200}
    by_provider = {r["key"]: r["total_tokens"] for r in s["by_provider"]}
    assert by_provider == {"openai": 150, "ollama": 200}


def test_sessions_grouping(db):
    svc = UsageService(db)
    svc.record(thread_id="t1", input_tokens=100, output_tokens=20, role="router")
    svc.record(thread_id="t1", input_tokens=200, output_tokens=50, role="final_answer")
    svc.record(thread_id="t2", input_tokens=10, output_tokens=5, role="router")
    sessions = svc.sessions()
    by_id = {s["thread_id"]: s for s in sessions}
    assert by_id["t1"]["total_tokens"] == 370
    assert by_id["t1"]["by_stage"] == {"router": 120, "final_answer": 250}
    assert by_id["t2"]["total_tokens"] == 15
    assert by_id["t2"]["by_stage"] == {"router": 15}
    # most-recently-used thread first (t2 recorded last)
    assert sessions[0]["thread_id"] == "t2"
    assert all(s["last_used"] is not None for s in sessions)


def test_sessions_role_defaults_to_unknown(db):
    svc = UsageService(db)
    svc.record(thread_id="t1", input_tokens=100, output_tokens=0)
    sessions = svc.sessions()
    assert sessions[0]["by_stage"] == {"unknown": 100}


def test_sessions_collapse_fine_grained_roles(db):
    svc = UsageService(db)
    # Per-turn role tags carry identifiers; they collapse to coarse stages.
    svc.record(thread_id="t1", input_tokens=100, output_tokens=0, role="router:1")
    svc.record(thread_id="t1", input_tokens=200, output_tokens=0, role="router:2")
    svc.record(thread_id="t1", input_tokens=50, output_tokens=0, role="writer:asst:3")
    svc.record(thread_id="t1", input_tokens=30, output_tokens=0, role="memory_decision:router")
    by_stage = svc.sessions()[0]["by_stage"]
    assert by_stage == {"router": 300, "writer": 50, "memory_decision": 30}


def test_summary_by_stage(db):
    svc = UsageService(db)
    svc.record(thread_id="a", input_tokens=100, output_tokens=0, role="router:1")
    svc.record(thread_id="b", input_tokens=40, output_tokens=0, role="router:2")
    svc.record(thread_id="a", input_tokens=10, output_tokens=0, role="cognition:planner")
    s = svc.summary()
    by_stage = {r["key"]: r["total_tokens"] for r in s["by_stage"]}
    assert by_stage == {"router": 140, "cognition": 10}
    # ordered most tokens first
    assert s["by_stage"][0]["key"] == "router"


def test_budget_set_and_status(db):
    svc = UsageService(db)
    svc.record(thread_id="t", input_tokens=400, output_tokens=100)
    assert svc.budget_status()["monthly_token_budget"] is None  # unset
    svc.set_budget(monthly_token_budget=1000, monthly_cost_budget_usd=5.0)
    b = svc.budget_status()
    assert b["monthly_token_budget"] == 1000
    assert b["used_tokens_this_month"] == 500
    assert b["tokens_left_this_month"] == 500
    assert b["over_budget"] is False
    svc.record(thread_id="t", input_tokens=600, output_tokens=0)
    b2 = svc.budget_status()
    assert b2["used_tokens_this_month"] == 1100
    assert b2["tokens_left_this_month"] == 0
    assert b2["over_budget"] is True
