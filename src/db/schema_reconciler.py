"""
db/schema_reconciler.py

Add-only schema reconciliation for zero-downtime upgrades.

`Base.metadata.create_all()` creates MISSING TABLES on an existing database but
never alters existing ones — so a new column on an existing model would be
absent in older databases and crash the upgraded app. This reconciler closes
that gap generically: on startup it diffs the SQLAlchemy models against the live
schema and issues `ALTER TABLE ... ADD COLUMN` for every model column the DB is
missing.

Guarantees:
- ADD-ONLY. It never drops, renames, retypes or reorders existing columns, so no
  data is ever lost and old rows keep working.
- Safe on populated tables: a column is added NOT NULL only when a default can be
  rendered; otherwise it is added nullable (logged), because a NOT NULL column
  without a default cannot be added to a table that already has rows.
- Out of scope (still needs an explicit hand-written migration): type changes,
  renames, backfills, constraint/index changes and column drops.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Session
from sqlalchemy.schema import Column, MetaData

from component.logging import get_logger
from db.database import Base

log = get_logger(__name__)


def _render_default(col: Column, dialect: Dialect) -> Optional[str]:
    """Best-effort SQL literal for a column's default, or None if we can't render
    one safely. Only used to add a NOT NULL column to an existing table."""
    server_default = getattr(col, "server_default", None)
    if server_default is not None and getattr(server_default, "arg", None) is not None:
        arg = server_default.arg
        try:
            return str(arg.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        except Exception:  # noqa: BLE001
            txt = getattr(arg, "text", None)
            return str(txt) if txt is not None else None
    default = getattr(col, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
    return None


def _add_column_ddl(table_name: str, col: Column, dialect: Dialect) -> str:
    coltype = col.type.compile(dialect=dialect)
    ddl = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {coltype}"
    default_sql = _render_default(col, dialect)
    # A NOT NULL column can only be added to a populated table with a default.
    if not col.nullable and default_sql is not None:
        ddl += " NOT NULL"
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
    return ddl


def reconcile_schema(db: Session, metadata: MetaData | None = None) -> list[str]:
    """Add every model column that the live database is missing. Returns the list
    of applied DDL statements (for logging/tests)."""
    metadata = metadata if metadata is not None else Base.metadata
    inspector = inspect(db.bind)
    dialect = db.bind.dialect
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception as exc:  # noqa: BLE001
        log.warningx("schema_reconcile:inspect_failed", error=str(exc))
        return []

    applied: list[str] = []
    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all() owns brand-new tables
        try:
            db_cols = {c["name"] for c in inspector.get_columns(table.name)}
        except Exception as exc:  # noqa: BLE001
            log.warningx("schema_reconcile:columns_failed", table=table.name, error=str(exc))
            continue
        for col in table.columns:
            if col.name in db_cols:
                continue
            if not col.nullable and _render_default(col, dialect) is None:
                log.warningx(
                    "schema_reconcile:adding_non_null_as_nullable",
                    table=table.name, column=col.name,
                    reason="NOT NULL without a renderable default cannot be added to existing rows",
                )
            ddl = _add_column_ddl(table.name, col, dialect)
            try:
                db.execute(text(ddl))
                db.commit()
                applied.append(ddl)
                log.infox("schema_reconcile:column_added", table=table.name, column=col.name, ddl=ddl)
            except Exception as exc:  # noqa: BLE001 — one bad column must not block startup
                db.rollback()
                log.warningx("schema_reconcile:add_column_failed", table=table.name, column=col.name, ddl=ddl, error=str(exc))
    if applied:
        log.infox("schema_reconcile:done", added=len(applied))
    return applied
