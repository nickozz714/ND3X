"""Planner shape coercion: tolerate models that wrap the plan in a JSON array (or emit
several objects) instead of returning a single object."""
from __future__ import annotations

from services.assistants.orchestration.formatting import _coerce_plan_to_dict


def test_single_element_list_is_unwrapped():
    assert _coerce_plan_to_dict([{"action": "tool_calls"}]).get("action") == "tool_calls"


def test_multi_element_list_picks_the_plan_like_dict():
    plan = _coerce_plan_to_dict([{"foo": 1}, {"action": "final", "final_answer": "hi"}])
    assert plan.get("action") == "final" and plan.get("final_answer") == "hi"


def test_list_without_a_dict_is_an_error_shape():
    assert "_plan_error" in _coerce_plan_to_dict(["a", "b"])


def test_plain_dict_passes_through():
    assert _coerce_plan_to_dict({"action": "ask_user"}).get("action") == "ask_user"
