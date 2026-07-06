"""Code-authoritative system skills: override resolution + capability addendum."""
from __future__ import annotations

from services.assistants.runtime.system_skills import (
    SYSTEM_SKILL_NAMES,
    is_system_skill,
    skill_override,
)


def test_known_system_skills_present():
    assert "orchestrator_tool_call_contract" in SYSTEM_SKILL_NAMES
    assert "runtime_file_artifact_inspection" in SYSTEM_SKILL_NAMES
    assert is_system_skill("orchestrator_response_contract") is True
    assert is_system_skill("some_user_skill") is False


def test_override_returns_code_content():
    ov = skill_override("orchestrator_response_contract")
    assert ov is not None
    assert ov["instructions"].strip()  # non-empty code content
    assert isinstance(ov["description"], str)


def test_tool_call_contract_has_capability_addendum():
    ov = skill_override("orchestrator_tool_call_contract")
    text = ov["instructions"]
    assert "agent__dispatch" in text
    assert "task__create" in text
    assert "parallel" in text.lower() or "concurrently" in text.lower()


def test_unknown_skill_returns_none():
    assert skill_override("not_a_system_skill") is None
