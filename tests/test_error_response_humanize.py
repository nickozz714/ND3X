"""A failed turn must never surface a raw traceback / provider dump in the chat:
the error response builds cleanly (trace is list[dict]) and the answer is friendly."""
from __future__ import annotations

from services.assistants.ask_job_callbacks import (
    error_ask_response,
    humanize_error_text,
    normalize_error_answer,
    _coerce_trace,
)

_OLLAMA_404 = (
    "Error code: 404 - {'error': {'message': \"model 'qwen2.5:32b' not found\", "
    "'type': 'not_found_error', 'param': None, 'code': None}}"
)


def test_error_response_builds_and_is_friendly():
    # Previously crashed: trace held a raw traceback string (schema wants list[dict]).
    resp = error_ask_response("thread-1", RuntimeError(_OLLAMA_404))
    assert resp["mode"] == "error"
    assert "Response build error" not in resp["answer"]
    assert "Traceback" not in resp["answer"]
    assert "Model not available" in resp["answer"]
    assert "qwen2.5:32b" in resp["answer"]
    # trace must be a list of dicts (valid AskResponse).
    assert isinstance(resp["trace"], list)
    assert all(isinstance(t, dict) for t in resp["trace"])


def test_humanize_known_errors():
    assert "Model not available" in humanize_error_text(_OLLAMA_404)
    assert "Can't reach the model" in humanize_error_text("Connection refused")
    assert "took too long" in humanize_error_text("Request timed out").lower()
    # Unknown error → generic friendly, no traceback leakage.
    g = humanize_error_text("weird boom")
    assert "Something went wrong" in g and "Traceback" not in g


def test_normalize_error_answer_only_rewrites_raw():
    raw = {"mode": "error", "answer": f"**Error:** {_OLLAMA_404}", "thread_id": "t"}
    assert "Model not available" in normalize_error_answer(raw)["answer"]
    # Already-friendly messages are left untouched.
    friendly = {"mode": "error", "answer": "⚠️ **Model not available**\n\nfoo", "thread_id": "t"}
    assert normalize_error_answer(friendly)["answer"].startswith("⚠️")
    # Non-error results pass through unchanged.
    ok = {"mode": "final", "answer": "here you go", "thread_id": "t"}
    assert normalize_error_answer(ok) is ok


def test_coerce_trace_wraps_non_dicts():
    out = _coerce_trace(["raw string", {"type": "x"}, 42])
    assert out == [
        {"type": "trace", "message": "raw string"},
        {"type": "x"},
        {"type": "trace", "message": "42"},
    ]
