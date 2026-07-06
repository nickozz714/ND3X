"""System skills split into chat / workflow / shared flows: chat turns must not carry
workflow-only contracts and vice versa."""
from __future__ import annotations

from services.assistants.runtime.system_skills import system_skill_applies, system_skill_flow
from services.assistants.runtime_config import AssistantConfig, SkillConfig
from services.assistants.prompt_builder import PromptBuilder


def test_flow_classification():
    assert system_skill_flow("orchestrator_workflow_context_contract") == "workflow"
    assert system_skill_flow("orchestrator_downstream_handoff_contract") == "workflow"
    assert system_skill_flow("orchestrator_tool_call_contract") == "shared"
    assert system_skill_flow("unknown_contract") == "shared"  # safe default


def test_applies_by_flow():
    # workflow-only contract
    assert system_skill_applies("orchestrator_workflow_context_contract", is_workflow=True) is True
    assert system_skill_applies("orchestrator_workflow_context_contract", is_workflow=False) is False
    # shared always
    assert system_skill_applies("orchestrator_tool_call_contract", is_workflow=False) is True
    assert system_skill_applies("orchestrator_tool_call_contract", is_workflow=True) is True


def _agent_with_system_skills():
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.tools = []
    cfg.skills = [
        SkillConfig(id=1, name="orchestrator_tool_call_contract", description="d", instructions="TOOLCALL RULES", is_system=True, is_enabled=True),
        SkillConfig(id=2, name="orchestrator_workflow_context_contract", description="d", instructions="WORKFLOW CONTEXT RULES", is_system=True, is_enabled=True),
    ]
    return cfg


def test_chat_manifest_excludes_workflow_contract():
    man = PromptBuilder().render_skill_manifest(_agent_with_system_skills(), selected_skill_names=[], is_workflow=False)
    assert "orchestrator_tool_call_contract" in man
    assert "orchestrator_workflow_context_contract" not in man


def test_workflow_manifest_includes_workflow_contract():
    man = PromptBuilder().render_skill_manifest(_agent_with_system_skills(), selected_skill_names=[], is_workflow=True)
    assert "orchestrator_tool_call_contract" in man
    assert "orchestrator_workflow_context_contract" in man
