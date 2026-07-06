"""Single-agent skill-selection path (RouterWorkflow.run_single_agent / select_skills).

Async paths are driven via asyncio.run() so no pytest-asyncio config is needed.
The LLM call and runtime/pipeline are faked, so this is deterministic and cost-free.
"""
from __future__ import annotations

import asyncio
import json

from services.assistants.orchestration.routing import RouterWorkflow


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgent:
    def extract_first_json_object(self, text: str):
        return json.loads(text)


class _FakeLoader:
    def list_agent_skill_catalog(self):
        return [
            {"name": "pm_project_discovery", "description": "Find/inspect projects.", "tool_count": 2},
            {"name": "text_search", "description": "Search documents.", "tool_count": 1},
        ]


class _FakeRuntime:
    def __init__(self) -> None:
        self.runtime_loader = _FakeLoader()

    def get_single_agent_runtime_assistant(self):
        return _FakeAgent()


class _FakeOpenAI:
    def __init__(self, selection_json: str) -> None:
        self._selection_json = selection_json
        self.calls = []

    async def ask_orchestration_async(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return _Resp(self._selection_json)


def _build(selection_json, pipeline_result=None, pipeline_calls=None):
    async def _pipeline(**kwargs):
        if pipeline_calls is not None:
            pipeline_calls.append(kwargs)
        return pipeline_result or {"mode": "synthesize_answer", "answer": "done"}

    return RouterWorkflow(
        runtime_resolver=_FakeRuntime(),
        openai=_FakeOpenAI(selection_json),
        output_validator=None,
        workflow_service=None,
        run_assistant_pipeline=_pipeline,
        trace_fn=lambda *a, **k: None,
    )


def test_run_single_agent_folds_selection_into_the_loop():
    """No separate selector call: run_single_agent enters the ONE agent loop directly with
    _needs_skill_selection so the agent picks its own skill (or answers) in-loop. The
    conversation state is forwarded to the loop so memory still works."""
    pipeline_calls = []
    rw = _build("{}", pipeline_result={"mode": "answer", "answer": "done"}, pipeline_calls=pipeline_calls)
    acs = {"recent_messages": [{"role": "user", "content": "earlier message"}]}

    result = asyncio.run(rw.run_single_agent(
        question="hi", payload={"x": 1, "_active_conversation_state": acs},
        session_id="t1", model=None, trace=[], turn_id=1,
    ))

    assert result["answer"] == "done"
    assert len(pipeline_calls) == 1            # straight into the one agent loop
    assert len(rw.openai.calls) == 0           # NO separate selector LLM call
    p = pipeline_calls[0]["payload"]
    assert p["_needs_skill_selection"] is True
    assert p["_selected_skill_names"] == []
    assert p["_active_conversation_state"] == acs   # memory forwarded to the loop
    assert pipeline_calls[0]["assistant"].__class__.__name__ == "_FakeAgent"


def test_skill_catalog_shown_until_a_skill_is_selected():
    from services.assistants.runtime_config import AssistantConfig, SkillConfig
    from services.assistants.prompt_builder import PromptBuilder

    def _agent():
        cfg = AssistantConfig(id=None, name="Agent")
        cfg.schema = {}
        cfg.tools = []
        cfg.skills = [SkillConfig(id=1, name="pm_project_discovery",
                                  description="Find/inspect projects.", instructions="X",
                                  is_system=False, is_enabled=True)]
        return cfg

    pb = PromptBuilder()
    needs = pb.build_planner_prompt(assistant=_agent(), question="q",
                                    payload={"_needs_skill_selection": True})
    assert "Skill catalog" in needs and "pm_project_discovery" in needs and "select_skills" in needs

    # Once a skill is selected the catalog + select rule disappear (its tools take over).
    selected = pb.build_planner_prompt(assistant=_agent(), question="q",
                                       payload={"_needs_skill_selection": True,
                                                "_selected_skill_names": ["pm_project_discovery"]})
    assert "Skill catalog" not in selected
