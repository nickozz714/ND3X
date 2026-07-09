"""
models/board.py

The agent's Kanban board: a single global to-do board that the AGENT owns and
works from (not a user task manager). Both the agent (via board__* builtin
tools) and the user (via the board GUI tile) can CRUD items; workflows pull a
column's top-N items to work on.

One global board → items need no board_id. Columns are the fixed `status`
enum. `depends_on` feeds the "blocked" column (an item with unfinished
dependencies belongs there).
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.types import JSON
from sqlalchemy.sql import func

from db.database import Base


# Fixed columns. "blocked" holds items waiting on a dependency or an open
# question; "done" items stay as history.
BOARD_STATUSES = ("todo", "doing", "blocked", "done")

# Priority drives the top-N selection a workflow pulls. Ordinal for sorting;
# higher number = more urgent (urgent picked before low).
BOARD_PRIORITIES = ("low", "medium", "high", "urgent")
PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2, "urgent": 3}

# Who last created/changed an item — lets the GUI show provenance ("the agent
# added this") and the agent tell its own items from the user's.
BOARD_ORIGINS = ("agent", "user")


class BoardItem(Base):
    __tablename__ = "board_item"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="todo", index=True)
    priority = Column(String(16), nullable=False, default="medium")
    # "Done when …" — the acceptance criteria the agent should satisfy before
    # moving the item to done. Free text.
    acceptance = Column(Text, nullable=True)
    # Blocked-by: ids of other board items this one waits on.
    depends_on = Column(JSON, nullable=False, default=list)
    # Free metadata for overview/filtering in the GUI. Workflows do NOT select
    # on labels (top-N-per-column only) — this is human/agent context.
    labels = Column(JSON, nullable=False, default=list)
    # agent | user — who created it.
    origin = Column(String(16), nullable=False, default="agent")
    updated_by = Column(String(16), nullable=False, default="agent")
    # The outcome once done (what the agent produced / concluded).
    result = Column(Text, nullable=True)
    # Manual order within a column (drag-drop in the GUI); ties broken by this
    # then created_at. Lower = higher in the column.
    position = Column(Integer, nullable=False, default=100)
    # Optional links back to the run/thread that worked the item.
    workflow_run_id = Column(Integer, nullable=True, index=True)
    thread_id = Column(String(128), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
