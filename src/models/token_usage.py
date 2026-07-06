from __future__ import annotations

from sqlalchemy import Column, Float, Index, Integer, String, Boolean

from db.database import Base


class TokenUsage(Base):
    """Per-turn (or per-stage) token ledger. One row per recorded LLM usage event.

    `total_tokens` is stored denormalised for cheap aggregation. `estimated` marks
    rows derived from a token estimate (no provider usage returned) vs. actual
    provider-reported usage.
    """

    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(Float, nullable=False)  # epoch seconds

    thread_id = Column(String(255), nullable=False, index=True)
    turn_id = Column(Integer, nullable=True)
    role = Column(String(64), nullable=True)  # router / planner / writer / turn / ...

    provider_type = Column(String(64), nullable=True)
    model = Column(String(255), nullable=True, index=True)

    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)

    estimated = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("idx_token_usage_thread_ts", "thread_id", "ts"),
        Index("idx_token_usage_ts", "ts"),
    )


class ThreadCompaction(Base):
    """Latest running summary for a thread, produced when its context neared the
    model's window. The full thread is never deleted — this summary is injected
    into context so the model can continue from {summary + recent turns}."""

    __tablename__ = "thread_compaction"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(String(255), nullable=False, index=True)
    summary = Column(String, nullable=False)  # Text; SQLite stores unbounded
    created_at = Column(Float, nullable=False, default=0.0)


class UsageBudget(Base):
    """Single-row, user-set monthly budget counted down against the ledger.

    Provider account quotas aren't reliably fetchable, so this is config-driven.
    """

    __tablename__ = "usage_budget"

    id = Column(Integer, primary_key=True, autoincrement=True)
    monthly_token_budget = Column(Integer, nullable=True)   # tokens/month, or None = unset
    monthly_cost_budget_usd = Column(Float, nullable=True)  # USD/month, or None = unset
    updated_at = Column(Float, nullable=False, default=0.0)
