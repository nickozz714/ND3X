"""Plan conformity validation (TODO 1.1) — valid JSON that is semantically dead
(empty select_skills, tool_calls without calls, final without an answer) must be
rejected with actionable problems, not silently burn an agent hop."""
from __future__ import annotations

from services.assistants.plan_validator import validate_plan
from services.assistants.prompt_builder import PromptBuilder
from services.assistants.runtime_config import AssistantConfig

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["final", "tool_calls", "select_skills", "ask_user", "propose_plan"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}


def test_valid_final_plan_passes():
    plan = {"action": "final", "reason": "r", "final_answer": "Hello"}
    assert validate_plan(plan, _SCHEMA) == []


def test_valid_tool_calls_plan_passes():
    plan = {"action": "tool_calls", "reason": "r",
            "tool_calls": [{"tool_id": 1, "tool": "system__shell_exec", "args": {"cmd": "date"}}]}
    assert validate_plan(plan, _SCHEMA) == []


def test_empty_select_skills_rejected():
    # The exact baseline failure: select_skills with nothing selected.
    plan = {"action": "select_skills", "reason": "r", "selected_skill_names": []}
    problems = validate_plan(plan, _SCHEMA)
    assert any("selected_skill_names" in p for p in problems)


def test_tool_calls_without_calls_rejected():
    plan = {"action": "tool_calls", "reason": "r", "tool_calls": []}
    problems = validate_plan(plan, _SCHEMA)
    assert any("tool_calls" in p for p in problems)


def test_tool_call_without_name_rejected():
    plan = {"action": "tool_calls", "reason": "r", "tool_calls": [{"tool_id": 3, "args": {}}]}
    problems = validate_plan(plan, _SCHEMA)
    assert any("missing the tool name" in p for p in problems)


def test_empty_final_and_ask_user_left_to_pipeline_salvage():
    # The pipeline has dedicated fallbacks for these (ask_user → reason, workflow
    # → clean terminal failure) — the validator must not hijack them.
    assert validate_plan({"action": "final", "reason": "r", "final_answer": ""}, _SCHEMA) == []
    assert validate_plan({"action": "ask_user", "reason": "r", "final_answer": None}, _SCHEMA) == []


def test_schema_violation_reported():
    plan = {"action": "explode", "reason": "r", "final_answer": "x"}
    problems = validate_plan(plan, _SCHEMA)
    assert any("action" in p for p in problems)


def test_non_dict_rejected():
    assert validate_plan([1, 2, 3], _SCHEMA)


def test_no_schema_still_runs_semantic_checks():
    plan = {"action": "select_skills", "reason": "r", "selected_skill_names": []}
    assert validate_plan(plan, None)


def test_prompt_renders_validation_feedback_block():
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.schema = {}
    cfg.tools = []
    cfg.skills = []
    pb = PromptBuilder()
    for light in (False, True):
        prompt = pb.build_planner_prompt(
            assistant=cfg, question="q",
            payload={"_light_mode": light,
                     "_plan_validation_feedback": ["action='final' requires final_answer."]},
        )
        assert "YOUR PREVIOUS REPLY WAS REJECTED" in prompt
        assert "requires final_answer" in prompt
        # feedback must not ALSO leak into the payload dump
        assert "_plan_validation_feedback" not in prompt
