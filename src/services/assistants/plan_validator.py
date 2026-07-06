"""
services/assistants/plan_validator.py

Automatic validation of planner output (TODO 1.1): after the raw text parses as
JSON, the plan must also CONFORM — syntactically to the planner JSON schema and
semantically to the action contract. Small models regularly emit plans that are
valid JSON but semantically dead (action='select_skills' with an empty
selected_skill_names, action='tool_calls' without tool calls). Accepting those
silently wastes a full agent hop — ~100s on a local model — so the pipeline
rejects them with targeted feedback instead.

`validate_plan` returns a list of human-readable problems (empty = valid). The
pipeline turns problems into a corrective retry: the next planner hop sees the
problems verbatim and fixes its reply.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)

# Semantic requirements per action; the JSON schema alone can't express these
# conditionals (and small-model structured outputs satisfy the shape anyway).
# NOTE: final/ask_user/propose_plan with empty text are intentionally NOT
# validated here — the pipeline has dedicated salvage paths for those (ask_user
# falls back to `reason`; workflows turn an empty question into a clean
# terminal failure). This gate only rejects plans that would otherwise burn a
# hop doing nothing.


def _schema_problems(plan: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> List[str]:
    """Validate against the planner JSON schema when possible (best-effort —
    jsonschema is a hard dependency, but a broken/unknown schema must never
    block a turn)."""
    if not isinstance(schema, dict) or not schema:
        return []
    try:
        import jsonschema
        validator = jsonschema.Draft202012Validator(schema)
        problems: List[str] = []
        for err in validator.iter_errors(plan):
            path = ".".join(str(p) for p in err.absolute_path) or "(root)"
            problems.append(f"{path}: {err.message}")
            if len(problems) >= 5:  # enough signal to correct; keep feedback short
                break
        return problems
    except Exception as exc:  # noqa: BLE001 — validation must never break the turn
        log.warningx("Planner schema-validatie overgeslagen", error=str(exc))
        return []


def _semantic_problems(plan: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    action = (plan.get("action") or "").strip()

    if action == "tool_calls":
        calls = plan.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            problems.append(
                "action='tool_calls' requires a non-empty tool_calls list."
            )
        else:
            for i, call in enumerate(calls):
                if not isinstance(call, dict) or not (call.get("tool") or "").strip():
                    problems.append(f"tool_calls[{i}]: missing the tool name.")

    elif action == "select_skills":
        names = plan.get("selected_skill_names")
        if not isinstance(names, list) or not [n for n in names if isinstance(n, str) and n.strip()]:
            problems.append(
                "action='select_skills' requires selected_skill_names with at least one "
                "exact skill name from the skill catalog — an empty selection does nothing."
            )

    return problems


def validate_plan(plan: Any, schema: Optional[Dict[str, Any]] = None) -> List[str]:
    """All conformity problems for a parsed plan (empty list = valid).

    Semantic action-contract checks always run; JSON-schema validation runs when
    a schema is supplied. Problems are phrased so they can be fed back to the
    model verbatim as correction instructions.
    """
    if not isinstance(plan, dict):
        return ["The reply must be one JSON object."]
    problems = _semantic_problems(plan)
    problems.extend(_schema_problems(plan, schema))
    return problems
