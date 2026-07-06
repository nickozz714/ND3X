# src/db/database.py
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from component.config import settings

# Base must exist at import time (every model imports it); it needs no DB.
Base = declarative_base()

# The engine/session factory are built lazily on first use so the app can boot
# BEFORE a database is configured (first-time setup picks the location). They are
# rebuilt via reset_engine() when setup changes the live settings in-process.
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _build_engine() -> Engine:
    dialect = (settings.DB_DIALECT or "").strip().lower() or "sqlite"

    if dialect == "sqlite":
        sqlite_path = settings.SQLITE_PATH or "./db/nd3x.dev.db"
        resolved = Path(sqlite_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            f"sqlite:///{resolved}",
            future=True,
            echo=False,
            connect_args={"check_same_thread": False},  # belangrijk voor FastAPI dev
        )

    if dialect == "mysql":
        db_pass = urllib.parse.quote_plus(settings.DB_PASS or "")
        mysql_url = settings.MYSQL_URL or (
            f"mysql+pymysql://{settings.DB_USER}:{db_pass}@{settings.DB_HOST}:"
            f"{int(settings.DB_PORT or 3306)}/{settings.DB_NAME}?charset=utf8mb4"
        )
        return create_engine(
            mysql_url,
            future=True,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=int(settings.DB_POOL_RECYCLE or 300),
            pool_size=int(settings.DB_POOL_SIZE or 5),
            max_overflow=int(settings.DB_MAX_OVERFLOW or 10),
            pool_timeout=int(settings.DB_POOL_TIMEOUT or 30),
        )

    raise RuntimeError(f"Unsupported DB_DIALECT={dialect!r}. Use sqlite or mysql.")


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine()
        _session_factory = sessionmaker(
            bind=_engine, autocommit=False, autoflush=False, future=True
        )
    return _engine


def get_session_factory() -> sessionmaker:
    if _session_factory is None:
        get_engine()
    return _session_factory


def reset_engine() -> None:
    """Drop the cached engine so the next access rebuilds it from current
    settings. Used by first-time setup after it writes the DB config."""
    global _engine, _session_factory
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _session_factory = None


def SessionLocal() -> Session:
    """Callable kept for backwards compatibility: `db = SessionLocal()` and
    `session_factory=SessionLocal` both work, now with a lazy engine underneath."""
    return get_session_factory()()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
