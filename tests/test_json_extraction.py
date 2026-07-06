"""extract_first_json_object must survive local-model output: reasoning blocks,
code fences, and leading prose around the JSON the router/planner need."""
from __future__ import annotations

import pytest

from services.assistants.runtime.base import RuntimeAssistant


class _Assistant(RuntimeAssistant):
    def __init__(self):
        self.name = "test"
        self.instructions = ""


def _extract(text):
    return _Assistant().extract_first_json_object(text)


def test_plain_json():
    assert _extract('{"mode": "answer"}') == {"mode": "answer"}


def test_json_after_prose():
    assert _extract('Sure, here you go:\n{"mode": "plan"}') == {"mode": "plan"}


def test_json_in_code_fence():
    assert _extract('```json\n{"mode": "plan", "steps": []}\n```') == {"mode": "plan", "steps": []}


def test_think_block_with_braces_is_stripped():
    # qwen/deepseek-style reasoning that itself contains brace-like text, then the
    # real JSON — the think block must not derail the scan.
    text = "<think>I should output {something like a plan}</think>\n{\"mode\": \"answer\"}"
    assert _extract(text) == {"mode": "answer"}


def test_no_json_raises():
    with pytest.raises(ValueError):
        _extract("I cannot help with that.")


def test_repair_trailing_commas():
    assert _extract('{"mode": "answer", "steps": [1, 2,],}') == {"mode": "answer", "steps": [1, 2]}


def test_repair_single_quotes_and_fence():
    assert _extract("```json\n{'mode': 'plan'}\n```") == {"mode": "plan"}


def test_repair_smart_quotes():
    assert _extract('{“mode”: “answer”}') == {"mode": "answer"}
