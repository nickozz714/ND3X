"""Tests for the login brute-force rate limiter."""
from __future__ import annotations

from authentication import login_rate_limit as rl


def _reset(key: str):
    rl.record_success(key)


def test_locks_out_after_max_failures():
    key = "1.2.3.4:user@example.com"
    _reset(key)
    for _ in range(rl.MAX_FAILURES):
        assert rl.retry_after(key) == 0
        rl.record_failure(key)
    assert rl.retry_after(key) > 0  # locked now
    _reset(key)


def test_success_clears_failures():
    key = "1.2.3.4:other@example.com"
    _reset(key)
    rl.record_failure(key)
    rl.record_failure(key)
    rl.record_success(key)
    assert rl.retry_after(key) == 0


def test_independent_keys():
    a, b = "ip-a:a@x.com", "ip-b:b@x.com"
    _reset(a); _reset(b)
    for _ in range(rl.MAX_FAILURES):
        rl.record_failure(a)
    assert rl.retry_after(a) > 0
    assert rl.retry_after(b) == 0
    _reset(a); _reset(b)
