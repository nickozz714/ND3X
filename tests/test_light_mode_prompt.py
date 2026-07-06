"""Light mode — the compact planner prompt for small/local models.

payload['_light_mode'] is resolved per-turn by the pipeline (per-model
prompt_mode, auto = local) and drives: distilled core contract instead of the
orchestrator_* system contracts, brief tool manifests instead of full arg
schemas (except actively selected skills), a one-line schema summary instead of
the full schema dump, and no capabilities primer. See docs/light-mode.md.
"""
from __future__ import annotations

from types import SimpleNamespace

from services.assistants.runtime_config import AssistantConfig
from services.assistants.prompt_builder import PromptBuilder

_LIGHT_MARKER = "Core rules (light mode)"  # heading from agent.instruction.light.md


def _tool(tool_id: int, name: str, args: dict | None = None):
    return SimpleNamespace(
        id=tool_id,
        name=name,
        description=f"{name} description",
        argument=args or {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}},
            "required": ["cmd"],
        },
        tool_instructions="",
        is_enabled=True,
    )


def _skill(name: str, *, is_system: bool = False, is_runtime: bool = False, tools=None):
    return SimpleNamespace(
        name=name,
        description=f"{name} desc",
        instructions=f"{name} instructions",
        is_enabled=True,
        is_system=is_system,
        is_runtime=is_runtime,
        skill_files=None,
        tools=tools or [],
    )


def _agent() -> AssistantConfig:
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.schema = {
        "type": "object",
        "properties": {
            "action": {"enum": ["final", "tool_calls", "select_skills", "ask_user"]},
            "reason": {"type": "string"},
            "final_answer": {"type": ["string", "null"]},
        },
        "required": ["action", "reason"],
    }
    cfg.tools = [_tool(1, "system__shell_exec")]
    cfg.skills = [
        _skill("orchestrator_response_contract", is_system=True),
        _skill("my_domain_skill", tools=[_tool(7, "domain__do_thing")]),
    ]
    return cfg


def test_light_prompt_uses_distilled_contract_and_schema_summary():
    pb = PromptBuilder()
    prompt = pb.build_planner_prompt(assistant=_agent(), question="q", payload={"_light_mode": True})
    assert _LIGHT_MARKER in prompt
    # distilled contract replaces the orchestrator_* system contract text
    assert "orchestrator_response_contract instructions" not in prompt
    # full schema dump replaced by the terse field list (action enum included)
    assert '"properties"' not in prompt
    assert "action*(final|tool_calls|select_skills|ask_user)" in prompt


def test_full_prompt_unchanged_by_default():
    pb = PromptBuilder()
    prompt = pb.build_planner_prompt(assistant=_agent(), question="q", payload={})
    assert _LIGHT_MARKER not in prompt
    # full mode keeps the real schema dump and the system contract
    assert '"properties"' in prompt
    assert "orchestrator_response_contract instructions" in prompt


def test_light_always_on_block_is_compact_with_brief_args():
    pb = PromptBuilder()
    cfg = _agent()
    full = pb.render_always_on_tools_block(cfg)
    light = pb.render_always_on_tools_block(cfg, compact=True)
    assert len(light) < len(full)
    # full mode carries the JSON schema; light mode the brief param list
    assert '"properties"' in full
    assert '"properties"' not in light
    assert "args: cmd*, cwd" in light


def test_light_selected_skill_keeps_full_schema():
    pb = PromptBuilder()
    cfg = _agent()
    manifest = pb.render_skill_manifest(
        cfg, selected_skill_names=["my_domain_skill"], include_always_on=False, light=True
    )
    # actively selected skill tools keep the full arg schema
    assert '"properties"' in manifest
    assert "domain__do_thing" in manifest


def test_light_runtime_skill_tools_are_brief():
    pb = PromptBuilder()
    cfg = _agent()
    cfg.skills = [_skill("runtime_inspection", is_runtime=True, tools=[_tool(9, "file_inspect")])]
    manifest = pb.render_skill_manifest(
        cfg, selected_skill_names=["runtime_inspection"], include_always_on=False, light=True
    )
    # runtime kind ≠ selected kind → brief args even when actively listed
    assert "file_inspect" in manifest
    assert '"properties"' not in manifest
    assert "args: cmd*, cwd" in manifest


def test_brief_args_marks_required():
    assert PromptBuilder._brief_args(
        {"properties": {"a": {}, "b": {}}, "required": ["b"]}
    ) == "a, b*"
    assert PromptBuilder._brief_args({}) == ""
