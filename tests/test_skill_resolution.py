"""Tolerant skill-choice resolution (guidance-gated). Maps a tool name or a
near-miss to the SKILL that provides it, and recovers skills implied by referenced
tool_calls — fixing the common small-model "named the tool instead of the skill"
failure. Only runs when extra guidance is enabled (the handler gates on the flag)."""
from __future__ import annotations

from types import SimpleNamespace

from services.assistants.orchestration.pipeline_runner import _resolve_skill_choices, _backfill_tool_ids


def _assistant():
    tool = SimpleNamespace(name="fabric_data_agent_query")
    skill = SimpleNamespace(
        name="fabric_operations_management",
        is_enabled=True, is_system=False, is_runtime=False,
        tools=[tool],
    )
    return SimpleNamespace(config=SimpleNamespace(skills=[skill]))


def test_exact_skill_name_kept():
    chosen, _ = _resolve_skill_choices(_assistant(), ["fabric_operations_management"], [])
    assert chosen == ["fabric_operations_management"]


def test_tool_name_maps_to_its_skill():
    # The exact failure from the audit: model put the TOOL name where a skill goes.
    chosen, dbg = _resolve_skill_choices(_assistant(), ["fabric_data_agent_query"], [])
    assert chosen == ["fabric_operations_management"]
    assert dbg["fabric_data_agent_query"].startswith("tool")


def test_case_insensitive_skill_match():
    chosen, _ = _resolve_skill_choices(_assistant(), ["Fabric_Operations_Management"], [])
    assert chosen == ["fabric_operations_management"]


def test_recovers_skill_from_referenced_tool_calls():
    chosen, _ = _resolve_skill_choices(_assistant(), [], ["fabric_data_agent_query"])
    assert chosen == ["fabric_operations_management"]


def test_unknown_name_dropped():
    chosen, dbg = _resolve_skill_choices(_assistant(), ["does_not_exist"], [])
    assert chosen == []
    assert dbg["does_not_exist"] == "dropped"


def test_system_and_runtime_skills_excluded():
    sys_skill = SimpleNamespace(name="sys", is_enabled=True, is_system=True, is_runtime=False, tools=[])
    a = SimpleNamespace(config=SimpleNamespace(skills=[sys_skill]))
    chosen, dbg = _resolve_skill_choices(a, ["sys"], [])
    assert chosen == [] and dbg["sys"] == "dropped"


# --- tool_id resolution by name (always on) ----------------------------------
def _assistant_with_builtin():
    # fabric_data_agent_query is a builtin always-on tool (config.tools), id 317.
    tool = SimpleNamespace(id=317, name="fabric_data_agent_query")
    return SimpleNamespace(config=SimpleNamespace(skills=[], tools=[tool]))


def test_wrong_tool_id_corrected():
    # The audit failure: right name, hallucinated id (2/171) → corrected to the real 317.
    out = _backfill_tool_ids(_assistant_with_builtin(), [{"tool": "fabric_data_agent_query", "tool_id": 2}])
    assert out[0]["tool_id"] == 317


def test_missing_tool_id_filled():
    out = _backfill_tool_ids(_assistant_with_builtin(), [{"tool": "fabric_data_agent_query"}])
    assert out[0]["tool_id"] == 317


def test_correct_tool_id_is_noop():
    out = _backfill_tool_ids(_assistant_with_builtin(), [{"tool": "fabric_data_agent_query", "tool_id": 317}])
    assert out[0]["tool_id"] == 317


def test_ambiguous_name_left_untouched():
    # Same tool name on two ids → can't safely resolve; keep the model's id.
    a = SimpleNamespace(config=SimpleNamespace(
        skills=[SimpleNamespace(name="s", is_enabled=True, is_system=False, is_runtime=False,
                                tools=[SimpleNamespace(id=10, name="dup"), SimpleNamespace(id=11, name="dup")])],
        tools=[],
    ))
    out = _backfill_tool_ids(a, [{"tool": "dup", "tool_id": 99}])
    assert out[0]["tool_id"] == 99


def test_unknown_tool_name_left_untouched():
    out = _backfill_tool_ids(_assistant_with_builtin(), [{"tool": "mystery_tool", "tool_id": 5}])
    assert out[0]["tool_id"] == 5
