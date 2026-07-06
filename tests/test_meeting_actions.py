"""Meeting-driven actions (#9) — pure-helper unit tests.

Covers the deterministic bits of the detector lane: robust JSON parsing of model
output, the compact state brief, web-search result shaping, and that an absent/
disabled policy is treated as "actions off". The LLM call + tool dispatch are
covered by integration, not here.
"""
from __future__ import annotations

from services.voice import meeting_action_service as mas


def test_parse_actions_plain_array():
    out = mas._parse_actions('[{"type":"lookup","topic":"Acme","query":"Acme Corp","confidence":0.8}]')
    assert len(out) == 1 and out[0]["topic"] == "Acme"


def test_parse_actions_strips_code_fence_and_prose():
    text = 'Sure!\n```json\n[{"type":"answer","query":"x","confidence":0.9}]\n```'
    out = mas._parse_actions(text)
    assert out and out[0]["type"] == "answer"


def test_parse_actions_wraps_single_object_and_handles_garbage():
    assert mas._parse_actions('{"type":"lookup","query":"x","confidence":0.7}')[0]["query"] == "x"
    assert mas._parse_actions("not json at all") == []
    assert mas._parse_actions("") == []


def test_state_brief_compacts_and_truncates():
    state = {"views": {"exec": "We discussed pricing."}, "open_questions": [{"text": "What is the budget?"}]}
    brief = mas._state_brief(state)
    assert "pricing" in brief and "budget" in brief.lower()
    assert mas._state_brief(None) == "(no notes yet)"


def test_shape_search_result_variants():
    body, sources, status = mas._shape_search_result({"ok": True, "answer": "Hello"})
    assert body == "Hello" and status == "done" and sources == []
    body, _, status = mas._shape_search_result({"ok": False, "error": "no key"})
    assert status == "error" and "no key" in body


def test_load_policy_disabled_or_absent_returns_none():
    # Unknown / code profile with no action_policy attribute → None (actions off).
    assert mas.load_policy("default_meeting") is None
    assert mas.load_policy(None) is None


def test_detector_model_only_from_dedicated_slot_no_fallback():
    # The detector resolves ONLY meeting.action_detector; an unassigned slot
    # means actions are OFF — it must NOT borrow other chat slots.
    class _Reg:
        def __init__(self, db): pass
        def resolve_slot(self, slot):
            # simulate: dedicated slot empty, but other slots DO have models
            return None if slot == "meeting.action_detector" else type("R", (), {"model_id": "gpt-x"})()
    import services.providers.registry_service as rs
    orig = rs.ProviderRegistryService
    rs.ProviderRegistryService = _Reg
    try:
        assert mas._resolve_detector_model(db=None) is None
    finally:
        rs.ProviderRegistryService = orig


def test_detector_model_used_when_slot_assigned():
    class _Reg:
        def __init__(self, db): pass
        def resolve_slot(self, slot):
            return type("R", (), {"model_id": "nano-1"})() if slot == "meeting.action_detector" else None
    import services.providers.registry_service as rs
    orig = rs.ProviderRegistryService
    rs.ProviderRegistryService = _Reg
    try:
        assert mas._resolve_detector_model(db=None) == "nano-1"
    finally:
        rs.ProviderRegistryService = orig
