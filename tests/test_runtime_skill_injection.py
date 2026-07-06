from services.assistants.orchestration.runtime_skill_injection import (
    RUNTIME_FILE_SKILL_NAME,
    resolve_effective_selected_skills,
    should_attach_file_artifact_runtime_skill,
)
from services.assistants.runtime_config import AssistantConfig, SkillConfig, ToolConfig
from services.assistants.prompt_builder import PromptBuilder
from services.assistants.tool_guard import AssistantToolGuard


def _assistant_with_skills():
    normal = SkillConfig(id=1, name="normal_domain", tools=[ToolConfig(id=11, name="normal_tool")])
    system = SkillConfig(id=2, name="system_global", is_system=True, tools=[ToolConfig(id=12, name="system_tool")])
    runtime = SkillConfig(id=3, name=RUNTIME_FILE_SKILL_NAME, is_runtime=True, tools=[ToolConfig(id=13, name="file_inspect")])
    return AssistantConfig(id=1, name="a", instruction="x", skills=[normal, system, runtime])


def test_no_file_intent_no_runtime_injection():
    assistant = _assistant_with_skills()
    out = resolve_effective_selected_skills(base_selected_skill_names=["normal_domain"], assistant_skills=assistant.skills, question="how are you", payload={})
    assert RUNTIME_FILE_SKILL_NAME not in out


def test_file_intent_injects_runtime_skill():
    assistant = _assistant_with_skills()
    out = resolve_effective_selected_skills(base_selected_skill_names=["normal_domain"], assistant_skills=assistant.skills, question="inspecteer dit bestand csv", payload={})
    assert RUNTIME_FILE_SKILL_NAME in out


def test_payload_content_ref_injects_runtime_skill():
    assistant = _assistant_with_skills()
    out = resolve_effective_selected_skills(base_selected_skill_names=["normal_domain"], assistant_skills=assistant.skills, question="continue", payload={"content_ref": "artifact://t/r/c/f.csv"})
    assert RUNTIME_FILE_SKILL_NAME in out


def test_previous_results_markers_inject_runtime_skill():
    assert should_attach_file_artifact_runtime_skill(question="continue", payload={"previous_step_results": [{"inspection_level": "artifact_only", "full_content_available_to_llm": False}]}) is True


def test_manifest_includes_runtime_only_when_injected():
    assistant = _assistant_with_skills()
    pb = PromptBuilder()
    m1 = pb.render_skill_manifest(assistant, selected_skill_names=["normal_domain"])
    assert RUNTIME_FILE_SKILL_NAME not in m1
    m2 = pb.render_skill_manifest(assistant, selected_skill_names=["normal_domain", RUNTIME_FILE_SKILL_NAME])
    assert RUNTIME_FILE_SKILL_NAME in m2


def test_system_skills_still_active_and_runtime_tools_guarded_by_selection():
    assistant = _assistant_with_skills()
    guard = AssistantToolGuard()
    names_without_runtime = guard.allowed_tool_names_for(assistant, selected_skill_names=["normal_domain"])
    assert "system_tool" in names_without_runtime
    assert "file_inspect" not in names_without_runtime
    names_with_runtime = guard.allowed_tool_names_for(assistant, selected_skill_names=["normal_domain", RUNTIME_FILE_SKILL_NAME])
    assert "file_inspect" in names_with_runtime
