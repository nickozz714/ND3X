"""tool_id is mandatory on every tool call.

Covers both layers:
- A: the planner output schema *requires* tool_id.
- B: a dropped/invalid tool_id is recovered from the manifest by name
     (_backfill_tool_ids), and internal capability tools (no DB id) execute by
     name regardless of a 0-sentinel, while a genuine dynamic tool without a real
     id still hard-stops.

Async paths use asyncio.run(); no pytest-asyncio needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import types

import pytest

from services.assistants.orchestration.pipeline_runner import _backfill_tool_ids
from services.assistants.orchestration.tool_execution import ToolExecutionRunner

SPEC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src", "services", "assistants", "runtime", "system_specs",
)


def _assistant_with_tools(*pairs):
    """Build a minimal assistant.config.skills[].tools[] with (name, id) pairs."""
    tools = [types.SimpleNamespace(name=n, id=i) for (n, i) in pairs]
    skill = types.SimpleNamespace(name="s", tools=tools)
    return types.SimpleNamespace(config=types.SimpleNamespace(skills=[skill], tools=[]))


def _assistant_with_builtin_tools(*pairs):
    """Builtin always-on tools live on config.tools (§2), not under a skill."""
    tools = [types.SimpleNamespace(name=n, id=i) for (n, i) in pairs]
    return types.SimpleNamespace(config=types.SimpleNamespace(skills=[], tools=tools))


# ── A: schema requires tool_id ────────────────────────────────────────────────

def test_planner_schema_requires_tool_id():
    with open(os.path.join(SPEC_DIR, "planner.schema.json")) as f:
        schema = json.load(f)
    required = schema["properties"]["tool_calls"]["items"]["required"]
    assert "tool_id" in required


# ── B: backfill recovers a dropped/invalid id by name ─────────────────────────

def test_backfill_fills_missing_tool_id_from_manifest():
    assistant = _assistant_with_tools(("text__search", 297))
    calls = [{"tool": "text__search", "tool_id": None, "args": {}}]
    _backfill_tool_ids(assistant, calls)
    assert calls[0]["tool_id"] == 297


def test_backfill_recovers_builtin_tool_id_from_config_tools():
    # Builtin always-on tools (§2) are on config.tools, not under a skill — a weaker
    # local model that drops the id on a builtin call must still be recovered.
    assistant = _assistant_with_builtin_tools(("text__search", 297), ("system__shell_exec", 999))
    calls = [
        {"tool": "text__search", "tool_id": None, "args": {}},
        {"tool": "system__shell_exec", "tool_id": 0, "args": {}},
    ]
    _backfill_tool_ids(assistant, calls)
    assert calls[0]["tool_id"] == 297
    assert calls[1]["tool_id"] == 999


def test_backfill_fixes_non_positive_id():
    assistant = _assistant_with_tools(("text__search", 297))
    calls = [{"tool": "text__search", "tool_id": 0, "args": {}}]
    _backfill_tool_ids(assistant, calls)
    assert calls[0]["tool_id"] == 297


def test_backfill_leaves_real_id_untouched():
    assistant = _assistant_with_tools(("text__search", 297), ("text__search", 998))
    calls = [{"tool": "text__search", "tool_id": 297, "args": {}}]
    _backfill_tool_ids(assistant, calls)
    assert calls[0]["tool_id"] == 297  # real id kept; ambiguity never overrides


def test_backfill_skips_unknown_and_ambiguous():
    # unknown name → left as-is; ambiguous name (2 ids) → not auto-resolved
    assistant = _assistant_with_tools(("dup", 1), ("dup", 2))
    calls = [
        {"tool": "not_in_manifest", "tool_id": None, "args": {}},
        {"tool": "dup", "tool_id": None, "args": {}},
    ]
    _backfill_tool_ids(assistant, calls)
    assert calls[0]["tool_id"] is None
    assert calls[1]["tool_id"] is None


# ── Execution: internal by name vs dynamic hard-stop ──────────────────────────

def _runner():
    return ToolExecutionRunner(
        tool_execution_service=types.SimpleNamespace(),
        ingest_wait_timeout_s=1.0,
        ingest_poll_interval_s=0.01,
        max_tool_calls_per_turn=10,
    )


def test_internal_tool_routes_by_name_with_sentinel_id(monkeypatch):
    from services.builtin.internal_tool_registry import internal_tool_registry as reg

    async def fake_call(name, args):
        return {"status": "ok", "routed": name}

    monkeypatch.setattr(reg, "has_tool", lambda n: n == "agent__dispatch")
    monkeypatch.setattr(reg, "call", fake_call)

    out = asyncio.run(_runner().call_tool_with_ingest_handling(
        {"tool": "agent__dispatch", "tool_id": 0}, {"task": "x"}
    ))
    assert out == {"status": "ok", "routed": "agent__dispatch"}


def test_dynamic_tool_without_real_id_hard_stops(monkeypatch):
    from services.builtin.internal_tool_registry import internal_tool_registry as reg
    monkeypatch.setattr(reg, "has_tool", lambda n: False)

    with pytest.raises(ValueError):
        asyncio.run(_runner().call_tool_with_ingest_handling(
            {"tool": "some_mcp_tool", "tool_id": None}, {}
        ))


# ── tool-name alias (single -> double underscore) ─────────────────────────────

def test_canonical_tool_name_aliases_single_to_double_underscore():
    from services.assistants.orchestration.tool_execution import canonical_tool_name
    assert canonical_tool_name("text_search") == "text__search"
    assert canonical_tool_name("text_ingest") == "text__ingest"
    assert canonical_tool_name("text__search") == "text__search"   # already canonical
    assert canonical_tool_name("some_other_tool") == "some_other_tool"  # untouched
