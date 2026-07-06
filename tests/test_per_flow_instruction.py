"""Per-flow agent instruction blocks (chat vs workflow) are distinct and flow-correct."""
from __future__ import annotations

from services.assistants.orchestration.pipeline_runner import _flow_instruction


def test_chat_block_allows_asking():
    chat = _flow_instruction(False)
    assert chat and "ask_user" in chat


def test_workflow_block_forbids_asking():
    wf = _flow_instruction(True)
    assert wf and "NEVER ask" in wf and "autonomous" in wf.lower()


def test_blocks_are_distinct():
    assert _flow_instruction(False) != _flow_instruction(True)
