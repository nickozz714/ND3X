"""§6 — stateful agent session.

On a stateful continuation hop the model already holds every prior hop in its
server-side Responses session, so the planner prompt must send only the new "_last_*"
delta and drop the full "_acc_*" accumulators (the old O(n^2) re-dump). On a stateless
hop (first pass / no session memory) the opposite holds: dump "_acc_*", drop "_last_*".
"""
from __future__ import annotations

from services.assistants.runtime_config import AssistantConfig
from services.assistants.prompt_builder import PromptBuilder


def _agent() -> AssistantConfig:
    cfg = AssistantConfig(id=None, name="Agent")
    cfg.schema = {}
    cfg.tools = []
    cfg.skills = []
    return cfg


def _payload() -> dict:
    return {
        "_acc_tool_results": [{"marker": "ACC_MARKER"}],
        "_acc_tool_calls": [{"marker": "ACC_MARKER"}],
        "_acc_docs": [{"marker": "ACC_MARKER"}],
        "_last_tool_results": [{"marker": "LAST_MARKER"}],
        "_last_tool_calls": [{"marker": "LAST_MARKER"}],
        "_last_docs": [{"marker": "LAST_MARKER"}],
    }


def test_stateful_continuation_sends_only_delta():
    payload = _payload()
    payload["_stateful_continuation"] = True
    prompt = PromptBuilder().build_planner_prompt(assistant=_agent(), question="q", payload=payload)
    assert "LAST_MARKER" in prompt          # the new observation is sent
    assert "ACC_MARKER" not in prompt       # the accumulated history is NOT re-dumped


def test_stateless_hop_dumps_full_accumulators():
    payload = _payload()
    payload["_stateful_continuation"] = False
    prompt = PromptBuilder().build_planner_prompt(assistant=_agent(), question="q", payload=payload)
    assert "ACC_MARKER" in prompt           # no session memory → dump the full history
    assert "LAST_MARKER" not in prompt      # ...and drop the duplicate delta


def test_history_anchor_drops_all_accumulators():
    """The transcript-path anchor carries NO accumulators (they're replayed as turns)."""
    payload = _payload()
    payload["_history_anchor"] = True
    prompt = PromptBuilder().build_planner_prompt(assistant=_agent(), question="q", payload=payload)
    assert "ACC_MARKER" not in prompt
    assert "LAST_MARKER" not in prompt
