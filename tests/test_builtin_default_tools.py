"""Builtin tools available by default (TODO §2): the agent's always-on tools render
into the skill manifest even when no skill is selected."""
from __future__ import annotations

from services.assistants.runtime_config import AssistantConfig, ToolConfig
from services.assistants.prompt_builder import PromptBuilder
from services.assistants.tool_guard import AssistantToolGuard


def _agent_with_builtin_tools():
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.skills = []
    cfg.tools = [
        ToolConfig(id=297, name="text__search", description="Search documents.", argument={}),
        ToolConfig(id=999, name="system__shell_exec", description="Run a shell command.", argument={}),
    ]
    return cfg


def test_always_on_builtin_tools_render_with_no_skill_selected():
    man = PromptBuilder().render_skill_manifest(_agent_with_builtin_tools(), selected_skill_names=[])
    assert "Always-available builtin tools" in man
    assert "text__search" in man and "tool_id=297" in man
    assert "system__shell_exec" in man and "tool_id=999" in man


def test_no_always_on_section_when_no_tools():
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.skills = []
    cfg.tools = []
    man = PromptBuilder().render_skill_manifest(cfg, selected_skill_names=[])
    assert "Always-available builtin tools" not in man


def test_guard_allows_always_on_builtin_tools_without_a_selected_skill():
    """Builtin tools are shown as always-available, so the guard must allow them even when
    no domain skill (or only an unrelated one) is selected — otherwise the agent gets
    'not allowed to call tool_id=...' for a tool it was told it could use."""
    guard = AssistantToolGuard()
    agent = _agent_with_builtin_tools()
    ids = guard.allowed_tool_ids_for(agent, selected_skill_names=[])
    assert 297 in ids and 999 in ids
    # No exception when calling a builtin tool with no skill selected.
    guard.assert_tools_allowed(
        agent,
        [{"tool": "text__search", "tool_id": 297, "args": {}}],
        selected_skill_names=[],
    )
