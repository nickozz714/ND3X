"""
services/assistants/runtime/system_skills.py

Code-authoritative system/runtime skills (the orchestrator contracts +
runtime file-inspection). Like the router/final_answer instructions, these must
NOT be changeable via the database and have no DB dependency for their content.

Content lives in ./system_specs/skills/<name>.md (+ _descriptions.json). The
loader (`runtime_config_loader._skill_to_config`) overrides DB instructions with
these for any system/runtime skill. They are also hidden from the workbench.

The tool-call contract is augmented in code with the newer execution
capabilities (parallel tool batches, subagent dispatch, background tasks).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

_SKILL_DIR = Path(__file__).parent / "system_specs" / "skills"

_DESCRIPTIONS: Dict[str, str] = json.loads((_SKILL_DIR / "_descriptions.json").read_text(encoding="utf-8"))
SYSTEM_SKILL_NAMES = set(_DESCRIPTIONS.keys())

# Which flow each system/runtime contract belongs to, so chat turns don't carry
# workflow-only instructions and vice versa. "shared" = injected on every turn;
# "workflow" = only on workflow (background) runs; "chat" = only on interactive chat.
SYSTEM_SKILL_FLOWS: Dict[str, str] = {
    "orchestrator_tool_call_contract": "shared",
    "orchestrator_response_contract": "shared",
    "orchestrator_completion_integrity_contract": "shared",
    "runtime_file_artifact_inspection": "shared",
    "orchestrator_workflow_context_contract": "workflow",
    "orchestrator_downstream_handoff_contract": "workflow",
}


def system_skill_flow(name: str) -> str:
    """The flow a system skill targets: 'shared' | 'workflow' | 'chat'. Unknown → shared."""
    return SYSTEM_SKILL_FLOWS.get((name or "").strip(), "shared")


def system_skill_applies(name: str, *, is_workflow: bool) -> bool:
    """Whether a system skill should be injected for the current flow."""
    flow = system_skill_flow(name)
    if flow == "shared":
        return True
    if flow == "workflow":
        return bool(is_workflow)
    if flow == "chat":
        return not is_workflow
    return True

# Capability updates kept in code so the contracts stay current with the engine.
_CAPABILITY_ADDENDUM: Dict[str, str] = {
    "orchestrator_tool_call_contract": (
        "\n\n## Advanced execution tools\n"
        "- You may emit MULTIPLE entries in `tool_calls` in one response. Independent "
        "calls (no `${result.N}`/`${last}` placeholder referencing another call) run "
        "concurrently; dependent calls run after the calls they reference.\n"
        "- `agent__dispatch` delegates a self-contained subtask to a fresh subagent and "
        "returns a condensed result (summary/facts/artifacts). Issue several in one "
        "response to fan work out in parallel.\n"
        "- `task__create` runs work in the background and returns a task_id immediately; "
        "poll `task__status` / `task__result` (or `task__list`) later. Completed "
        "background tasks are also surfaced to you automatically on later turns.\n"
        "Prefer the simplest approach; reach for delegation/background only when it helps."
    ),
}


def is_system_skill(name: str) -> bool:
    return (name or "") in SYSTEM_SKILL_NAMES


def skill_override(name: str) -> Optional[Dict[str, str]]:
    """Return {'description', 'instructions'} from code for a system skill, or None."""
    name = (name or "").strip()
    if name not in SYSTEM_SKILL_NAMES:
        return None
    instructions = (_SKILL_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip()
    addendum = _CAPABILITY_ADDENDUM.get(name)
    if addendum:
        instructions = instructions + addendum
    return {"description": _DESCRIPTIONS.get(name) or "", "instructions": instructions}
