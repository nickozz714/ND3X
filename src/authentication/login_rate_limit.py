"""In-memory brute-force protection for the login endpoint.

Tracks failed attempts per (client IP + email) and locks the key out for a
cooldown once a threshold is exceeded. Successful logins clear the counter.

In-process only (per worker) — good enough to blunt online brute-force/credential
stuffing without external infra. For a multi-node deployment back this with Redis.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

MAX_FAILURES = 5          # failures allowed within the window before lockout
WINDOW_SECONDS = 300      # rolling window for counting failures (5 min)
LOCKOUT_SECONDS = 900     # lockout duration once tripped (15 min)


@dataclass
class _Entry:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


_lock = threading.Lock()
_entries: dict[str, _Entry] = {}


def _now() -> float:
    return time.time()


def retry_after(key: str) -> int:
    """Seconds the caller must wait, or 0 if not currently locked."""
    with _lock:
        e = _entries.get(key)
        if not e:
            return 0
        remaining = int(e.locked_until - _now())
        return remaining if remaining > 0 else 0


def record_failure(key: str) -> None:
    with _lock:
        e = _entries.setdefault(key, _Entry())
        now = _now()
        e.failures = [t for t in e.failures if now - t < WINDOW_SECONDS]
        e.failures.append(now)
        if len(e.failures) >= MAX_FAILURES:
            e.locked_until = now + LOCKOUT_SECONDS
            e.failures = []


def record_success(key: str) -> None:
    with _lock:
        _entries.pop(key, None)
