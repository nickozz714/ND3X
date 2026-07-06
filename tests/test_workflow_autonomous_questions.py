"""Workflows are fully autonomous: the agent never asks the user. Any ask_user in a
workflow fails the operation; outside a workflow an empty question fails but a real
question pauses."""
from __future__ import annotations

from services.assistants.orchestration.pipeline_runner import _ask_user_should_fail


def test_workflow_never_asks_even_with_a_question():
    assert _ask_user_should_fail("Which project?", is_workflow=True) is True


def test_workflow_empty_question_fails():
    assert _ask_user_should_fail("   ", is_workflow=True) is True


def test_non_workflow_empty_question_fails():
    assert _ask_user_should_fail("", is_workflow=False) is True


def test_non_workflow_real_question_pauses():
    assert _ask_user_should_fail("Which project?", is_workflow=False) is False
