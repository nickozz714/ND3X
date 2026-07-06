"""Goal mode (/goal, TODO 4b) — the goal contract block is injected into the
planner prompt when payload['_goal_mode'] is set, and only then."""
from __future__ import annotations

from services.assistants.prompt_builder import PromptBuilder
from services.assistants.runtime_config import AssistantConfig

_MARKER = "GOAL MODE"  # heading from agent.instruction.goal.md


def _agent() -> AssistantConfig:
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.schema = {}
    cfg.tools = []
    cfg.skills = []
    return cfg


def test_goal_block_absent_by_default():
    prompt = PromptBuilder().build_planner_prompt(assistant=_agent(), question="q", payload={})
    assert _MARKER not in prompt


def test_goal_block_present_when_flag_set():
    prompt = PromptBuilder().build_planner_prompt(
        assistant=_agent(), question="q", payload={"_goal_mode": True}
    )
    assert _MARKER in prompt
    assert "demonstrably" in prompt.lower()


def test_goal_block_present_in_light_mode_too():
    prompt = PromptBuilder().build_planner_prompt(
        assistant=_agent(), question="q", payload={"_goal_mode": True, "_light_mode": True}
    )
    assert _MARKER in prompt
