"""Workflow builtin tools (skill = workflow launcher) + skill_ai workflow prompt."""
from __future__ import annotations

import asyncio

from services.builtin.tools import workflow_tools as wt
from services.assistants import skill_ai


def test_workflow_run_requires_workflow():
    out = asyncio.run(wt.workflow_run({}))
    assert out["status"] == "error" and "requires" in out["error"]


def test_skill_ai_prompt_includes_workflow_directive():
    prompt = skill_ai._build_prompt({"purpose": "x", "when": "y", "workflow": "Get latest news."})
    assert "workflow__run" in prompt
    assert "Get latest news." in prompt


def test_skill_ai_prompt_without_workflow_has_no_directive():
    prompt = skill_ai._build_prompt({"purpose": "x", "when": "y"})
    assert "workflow__run" not in prompt


def test_slugify_makes_valid_skill_name():
    assert skill_ai._slugify("Invoice Processing!") == "invoice_processing"
    assert skill_ai._slugify("") == "new_skill"
