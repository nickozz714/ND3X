"""Progressive extraction of the planner JSON's final_answer field, so the answer can be
streamed/shown growing while the planner call is still generating it."""
from __future__ import annotations

from services.assistants.orchestration.pipeline_runner import _extract_partial_final_answer as extract


def test_returns_growing_value_mid_stream():
    assert extract('{"action":"final","final_answer":"Hallo wer') == "Hallo wer"


def test_returns_complete_value():
    assert extract('{"action":"final","final_answer":"Hallo wereld"}') == "Hallo wereld"


def test_none_until_field_or_for_null():
    assert extract('{"action":"tool_calls"') is None        # field not present yet
    assert extract('{"final_answer":null}') is None          # null, not a string
    assert extract('{"final_answer":') is None               # no opening quote yet


def test_unescapes_common_sequences():
    assert extract('{"final_answer":"line1\\nline2') == "line1\nline2"
    assert extract('{"final_answer":"say \\"hi\\"') == 'say "hi"'
