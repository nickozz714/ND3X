"""
Dynamic Logging Library
=======================

A batteries-included, dependency-free logging utility for Python projects.

Environment Variables
---------------------
- LOG_LEVEL: DEBUG, INFO, WARNING, ERROR, CRITICAL (default INFO)
- LOG_FORMAT: text | json (default text)
- LOG_COLOR: 1/0 (default 1 when TTY)
- LOG_FILE: path to a rotating logfile (optional)
- LOG_ROTATE_MB: max file size in MB before rotation (default 10)
- LOG_BACKUP_COUNT: rotated file count to keep (default 5)
- LOG_SAMPLING_RATE: 0.0..1.0 probability to emit sampled logs (default 1)
- LOG_THROTTLE_SECS: minimum seconds between identical messages (default 0)
- LOG_CONTEXT: free-form string added as ``context`` field (optional)

Database Logging
----------------
- LOG_DB_ENABLED: 1/0 enable database log storage (default 0)
- LOG_DB_LEVEL: DEBUG, INFO, WARNING, ERROR, CRITICAL (default WARNING)
- LOG_DB_SAMPLE_RATE: 0.0..1.0 probability to persist DB logs (default 1)
- LOG_DB_MAX_FIELD_CHARS: max chars per text field stored in DB (default 12000)
- LOG_DB_EXCLUDE_LOGGERS: comma-separated logger name prefixes to skip (optional)
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Iterator, Mapping, Optional

from component.config import settings

# -------------------------
# Context propagation
# -------------------------
_ctx_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)
_ctx_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("span_id", default=None)
_ctx_user_ctx: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("user_ctx", default={})

ISO8601 = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO8601)


# -------------------------
# Formatting
# -------------------------
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": _now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for k in (
            "trace_id",
            "span_id",
            "sequence",
            "step",
            "duration_ms",
            "since_prev_ms",
            "context",
        ):
            v = getattr(record, k, None)
            if v is not None:
                data[k] = v

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, Mapping):
            data.update(extra_fields)

        if record.exc_info:
            data["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            data["exc"] = self.formatException(record.exc_info)

        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


class TextFormatter(logging.Formatter):
    LEVEL_TO_COLOR = {
        "DEBUG": 37,
        "INFO": 36,
        "WARNING": 33,
        "ERROR": 31,
        "CRITICAL": 41,
    }

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def _c(self, s: str, level: str) -> str:
        if not self.use_color:
            return s
        code = self.LEVEL_TO_COLOR.get(level, 37)
        return f"\033[{code}m{s}\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = _now_iso()
        level = record.levelname
        parts = [f"{ts}", self._c(level, level), f"{record.name}:", record.getMessage()]

        for k in (
            "trace_id",
            "span_id",
            "sequence",
            "step",
            "duration_ms",
            "since_prev_ms",
            "context",
        ):
            v = getattr(record, k, None)
            if v is not None:
                parts.append(f"{k}={v}")

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, Mapping) and extra_fields:
            ef = ", ".join(f"{k}={v}" for k, v in extra_fields.items())
            parts.append(f"{{{ef}}}")

        if record.exc_info:
            parts.append(self.formatException(record.exc_info))

        return " ".join(parts)


# -------------------------
# Configuration
# -------------------------
LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _default_use_color(stream: Any) -> bool:
    env_value = settings.LOG_COLOR
    if env_value is not None:
        return bool(env_value)
    return hasattr(stream, "isatty") and stream.isatty()


@dataclass
class LoggerConfig:
    name: str
    level: int = field(default_factory=lambda: LEVEL_MAP.get(settings.LOG_LEVEL.upper(), logging.INFO))
    fmt: str = field(default_factory=lambda: settings.LOG_FORMAT)
    stream: Any = sys.stdout
    file_path: Optional[str] = field(default_factory=lambda: settings.LOG_FILE)
    rotate_mb: int = field(default_factory=lambda: settings.LOG_ROTATE_MB)
    backup_count: int = field(default_factory=lambda: settings.LOG_BACKUP_COUNT)
    sampling_rate: float = field(default_factory=lambda: settings.LOG_SAMPLING_RATE)
    throttle_secs: float = field(default_factory=lambda: float(settings.LOG_THROTTLE_SECS))
    context: Optional[str] = field(default_factory=lambda: settings.LOG_CONTEXT)
    use_color: bool = field(default_factory=lambda: _default_use_color(sys.stdout))

    db_enabled: bool = field(default_factory=lambda: bool(settings.LOG_DB_ENABLED))
    db_level: int = field(default_factory=lambda: LEVEL_MAP.get(settings.LOG_DB_LEVEL.upper(), LEVEL_MAP["WARNING"]))
    db_sampling_rate: float = field(default_factory=lambda: settings.LOG_DB_SAMPLE_RATE)
    db_max_field_chars: int = field(default_factory=lambda: settings.LOG_DB_MAX_FIELD_CHARS)
    db_exclude_loggers: list[str] = field(default_factory=lambda: settings.LOG_DB_EXCLUDE_LOGGERS)


class _Throttler:
    def __init__(self, min_interval_sec: float):
        self.min_interval = max(0.0, float(min_interval_sec))
        self._last: Dict[str, float] = {}
        self._re = re.compile(r"\s+")

    def allow(self, key: str) -> bool:
        if self.min_interval <= 0:
            return True

        key = self._re.sub(" ", key.strip())
        now = time.monotonic()
        last = self._last.get(key, 0.0)

        if now - last >= self.min_interval:
            self._last[key] = now
            return True

        return False


class _Once:
    def __init__(self):
        self._seen: set[str] = set()

    def allow(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


# -------------------------
# Database handler
# -------------------------
def _safe_truncate(value: Any, max_chars: int) -> Optional[str]:
    if value is None:
        return None

    text = value if isinstance(value, str) else str(value)
    if len(text) <= max_chars:
        return text

    return text[:max_chars] + "…"


class DatabaseLogHandler(logging.Handler):
    """
    Persists log records to the database.

    Imports DB dependencies lazily to avoid circular imports while component.logging
    is imported during application startup.
    """

    def __init__(
        self,
        *,
        level: int,
        sampling_rate: float = 1.0,
        max_field_chars: int = 12000,
        exclude_loggers: Optional[list[str]] = None,
    ):
        super().__init__(level=level)
        self.sampling_rate = max(0.0, min(1.0, float(sampling_rate)))
        self.max_field_chars = max(100, int(max_field_chars))
        self.exclude_loggers = exclude_loggers or []

    def _is_excluded(self, logger_name: str) -> bool:
        return any(logger_name.startswith(prefix) for prefix in self.exclude_loggers)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._is_excluded(record.name):
                return

            # Cruciaal: voorkom circular import tijdens db.database startup.
            if record.name.startswith("db.database") or record.name.startswith("db.init_db"):
                return

            if self.sampling_rate < 1.0 and random.random() > self.sampling_rate:
                return

            extra_fields = getattr(record, "extra_fields", None)
            if not isinstance(extra_fields, Mapping):
                extra_fields = {}

            exc_type = None
            exc_text = None

            if record.exc_info:
                exc_type = record.exc_info[0].__name__ if record.exc_info[0] else None
                exc_text = self.formatException(record.exc_info)

            try:
                from db.database import SessionLocal
                from repository.log_repository import LogRepository
            except ImportError:
                # Database layer is still initializing. Skip this log record.
                return

            db = SessionLocal()
            try:
                LogRepository(db).create(
                    level=record.levelname,
                    logger=record.name,
                    message=_safe_truncate(record.getMessage(), self.max_field_chars) or "",
                    trace_id=getattr(record, "trace_id", None),
                    span_id=getattr(record, "span_id", None),
                    sequence=_safe_truncate(extra_fields.get("sequence"), 255),
                    step=_safe_truncate(extra_fields.get("step"), 255),
                    duration_ms=extra_fields.get("duration_ms"),
                    since_prev_ms=extra_fields.get("since_prev_ms"),
                    context=_safe_truncate(extra_fields.get("context"), self.max_field_chars),
                    extra_fields=dict(extra_fields),
                    exc_type=_safe_truncate(exc_type, 255),
                    exc_text=_safe_truncate(exc_text, self.max_field_chars),
                )
            finally:
                db.close()

        except Exception:
            self.handleError(record)


# -------------------------
# Public API
# -------------------------
class DynamicLogger(logging.Logger):
    """A standard logging.Logger with extras: context, sampling, throttling, and helpers."""

    def __init__(self, name: str, config: LoggerConfig):
        super().__init__(name, config.level)
        self.config = config
        self.setLevel(config.level)
        self.propagate = False

        self.handlers.clear()

        formatter = JSONFormatter() if config.fmt == "json" else TextFormatter(config.use_color)

        sh = logging.StreamHandler(config.stream)
        sh.setFormatter(formatter)
        sh.setLevel(config.level)
        self.addHandler(sh)

        if config.file_path:
            # Create the log directory on demand so a fresh checkout (no logs/
            # dir yet) boots instead of crashing in the file handler.
            log_dir = os.path.dirname(os.path.abspath(config.file_path))
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            fh = RotatingFileHandler(
                config.file_path,
                maxBytes=config.rotate_mb * 1024 * 1024,
                backupCount=config.backup_count,
            )
            fh.setFormatter(formatter)
            fh.setLevel(config.level)
            self.addHandler(fh)

        if config.db_enabled:
            dbh = DatabaseLogHandler(
                level=config.db_level,
                sampling_rate=config.db_sampling_rate,
                max_field_chars=config.db_max_field_chars,
                exclude_loggers=config.db_exclude_loggers,
            )
            dbh.setLevel(config.db_level)
            self.addHandler(dbh)

        self._throttler = _Throttler(config.throttle_secs)
        self._once = _Once()

    def bind(self, **fields: Any) -> "DynamicLogger":
        base = dict(_ctx_user_ctx.get())
        base.update(fields)
        token = _ctx_user_ctx.set(base)
        _ctx_user_ctx.reset(token)
        return self

    @contextmanager
    def context(self, **fields: Any) -> Iterator[None]:
        base = dict(_ctx_user_ctx.get())
        base.update(fields)
        token = _ctx_user_ctx.set(base)
        try:
            yield
        finally:
            _ctx_user_ctx.reset(token)

    def set_trace(self, trace_id: Optional[str] = None) -> "DynamicLogger":
        _ctx_trace_id.set(trace_id or new_trace_id())
        return self

    @contextmanager
    def span(self, span_id: Optional[str] = None) -> Iterator[None]:
        token = _ctx_span_id.set(span_id or new_span_id())
        try:
            yield
        finally:
            _ctx_span_id.reset(token)

    def _maybe_emit(
        self,
        log_level: int,
        msg: str,
        *,
        once_key: Optional[str] = None,
        throttle_key: Optional[str] = None,
        sampled: bool = True,
        exc_info: Any = None,
        **extra_fields: Any,
    ) -> None:
        if sampled and self.config.sampling_rate < 1.0 and random.random() > self.config.sampling_rate:
            return

        if throttle_key and not self._throttler.allow(throttle_key):
            return

        if once_key and not self._once.allow(once_key):
            return

        combined_extra = dict(_ctx_user_ctx.get())
        combined_extra.update(extra_fields)

        if self.config.context and "context" not in combined_extra:
            combined_extra["context"] = self.config.context

        super().log(
            log_level,
            msg,
            exc_info=exc_info,
            extra={
                "trace_id": _ctx_trace_id.get(),
                "span_id": _ctx_span_id.get(),
                "extra_fields": combined_extra or None,
            },
        )

    def infox(self, msg: str, *, once: bool = False, throttle: bool = False, **extra_fields: Any) -> None:
        self._maybe_emit(logging.INFO, msg, once_key=msg if once else None, throttle_key=msg if throttle else None, **extra_fields)

    def debugx(self, msg: str, *, once: bool = False, throttle: bool = False, **extra_fields: Any) -> None:
        self._maybe_emit(logging.DEBUG, msg, once_key=msg if once else None, throttle_key=msg if throttle else None, **extra_fields)

    def warningx(self, msg: str, *, once: bool = False, throttle: bool = False, **extra_fields: Any) -> None:
        self._maybe_emit(logging.WARNING, msg, once_key=msg if once else None, throttle_key=msg if throttle else None, **extra_fields)

    def errorx(
        self,
        msg: str,
        *,
        once: bool = False,
        throttle: bool = False,
        exc_info: Any = None,
        **extra_fields: Any,
    ) -> None:
        self._maybe_emit(
            logging.ERROR,
            msg,
            once_key=msg if once else None,
            throttle_key=msg if throttle else None,
            exc_info=exc_info,
            **extra_fields,
        )

    def exceptionx(self, msg: str, *, once: bool = False, throttle: bool = False, **extra_fields: Any) -> None:
        self.errorx(msg, once=once, throttle=throttle, exc_info=True, **extra_fields)


# Factory
_LOGGER_CACHE: Dict[str, DynamicLogger] = {}


def get_logger(name: str, **overrides: Any) -> DynamicLogger:
    cfg = LoggerConfig(name=name)

    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)

    if name in _LOGGER_CACHE:
        lg = _LOGGER_CACHE[name]
        if overrides:
            lg.__init__(name, cfg)  # type: ignore[misc]
        return lg

    lg = DynamicLogger(name, cfg)
    _LOGGER_CACHE[name] = lg
    return lg


def reconfigure_logging() -> None:
    """Rebuild every cached logger from the current `settings` (LOG_*). Called
    after DB-backed settings are hydrated at startup so log level/format/file
    reflect the database. Loggers already handed out are reconfigured in place."""
    for name, lg in list(_LOGGER_CACHE.items()):
        try:
            lg.__init__(name, LoggerConfig(name=name))  # type: ignore[misc]
        except Exception:
            pass


# -------------------------
# Step timing
# -------------------------
@dataclass
class _Step:
    name: str
    start_monotonic: float
    start_iso: str


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


class StepSequence:
    def __init__(self, logger: DynamicLogger, sequence_name: str):
        self.log = logger
        self.sequence = sequence_name
        self._prev_end: Optional[float] = None

    @contextmanager
    def step(self, step_name: str, **extra_fields: Any) -> Iterator[None]:
        start = time.monotonic()
        step_obj = _Step(name=step_name, start_monotonic=start, start_iso=_now_iso())

        self.log.debugx("step:start", sequence=self.sequence, step=step_obj.name, **extra_fields)

        try:
            yield
        except Exception:
            elapsed = time.monotonic() - start
            since_prev = (start - self._prev_end) if self._prev_end is not None else None

            self.log.errorx(
                "step:error",
                sequence=self.sequence,
                step=step_obj.name,
                duration_ms=_ms(elapsed),
                since_prev_ms=_ms(since_prev) if since_prev is not None else None,
                exc_info=True,
            )
            raise
        else:
            end = time.monotonic()
            elapsed = end - start
            since_prev = (start - self._prev_end) if self._prev_end is not None else None
            self._prev_end = end

            self.log.infox(
                "step:end",
                sequence=self.sequence,
                step=step_obj.name,
                duration_ms=_ms(elapsed),
                since_prev_ms=_ms(since_prev) if since_prev is not None else None,
            )


# -------------------------
# Decorators / context managers
# -------------------------
def log_step(logger: DynamicLogger, sequence: str, step: Optional[str] = None):
    seq = StepSequence(logger, sequence)

    def decorator(func):
        name = step or func.__name__

        def wrapper(*args, **kwargs):
            with seq.step(name):
                return func(*args, **kwargs)

        return wrapper

    return decorator


class log_context(AbstractContextManager):
    def __init__(self, **fields: Any):
        self.fields = fields
        self._token: Optional[contextvars.Token] = None

    def __enter__(self):
        base = dict(_ctx_user_ctx.get())
        base.update(self.fields)
        self._token = _ctx_user_ctx.set(base)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _ctx_user_ctx.reset(self._token)
        return False


# -------------------------
# IDs
# -------------------------
def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]
