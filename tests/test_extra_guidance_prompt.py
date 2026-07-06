"""Extra guidance — the opt-in "for dummies" instruction block is appended to the
planner prompt only when payload['_extra_guidance'] is set. The flag itself is
resolved per-turn by the pipeline from the model's per-model toggle (AI Models →
Routing) or a per-session override from the Chat tile."""
from __future__ import annotations

from services.assistants.runtime_config import AssistantConfig
from services.assistants.prompt_builder import PromptBuilder

_MARKER = "EXTRA GUIDANCE"  # heading from agent.instruction.guidance.md


def _agent() -> AssistantConfig:
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.schema = {}
    cfg.tools = []
    cfg.skills = []
    return cfg


def test_guidance_absent_by_default():
    prompt = PromptBuilder().build_planner_prompt(assistant=_agent(), question="q", payload={})
    assert _MARKER not in prompt


def test_guidance_present_when_flag_set():
    prompt = PromptBuilder().build_planner_prompt(
        assistant=_agent(), question="q", payload={"_extra_guidance": True}
    )
    assert _MARKER in prompt
    # The skill-vs-tool rule is the whole point — make sure it's there.
    assert "a SKILL is not a TOOL" in prompt
    assert "a TOOL, not a skill" in prompt
