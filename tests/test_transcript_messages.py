"""Slice 4: the transcript path replays accumulated hops as a real conversation
(anchor + assistant tool-call turns + user observation turns), identical on every
provider, instead of dumping the _acc_* blob into one prompt."""
from __future__ import annotations

from services.assistants.orchestration.pipeline_runner import _build_transcript_messages


class _FakeAssistant:
    def prompt(self, question, **payload):
        # The anchor must be built with accumulators suppressed.
        tag = "ANCHOR" if payload.get("_history_anchor") else "FULL"
        return f"{tag}:{question}"


def test_first_hop_is_just_the_prompt():
    msgs = _build_transcript_messages(_FakeAssistant(), "q", {}, "PLAN_PROMPT")
    assert msgs == [{"role": "user", "content": "PLAN_PROMPT"}]


def test_replays_accumulated_hops_as_turns():
    payload = {
        "_acc_tool_calls": [{"tool": "text_search", "args": {"q": "x"}}],
        "_acc_tool_results": [{"tool": "text_search", "status": "ok", "summary": "found-it"}],
        "_acc_docs": [{"path": "a.md"}],
    }
    msgs = _build_transcript_messages(_FakeAssistant(), "q", payload, "PLAN_PROMPT")

    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user", "user"]
    assert msgs[0]["content"].startswith("ANCHOR")     # anchor suppresses accumulators
    assert "text_search" in msgs[1]["content"]          # assistant tool-call turn
    assert "found-it" in msgs[2]["content"]             # user observation turn
    assert "a.md" in msgs[3]["content"]                 # docs turn
    assert "Continue" in msgs[4]["content"]             # next-action nudge
    # every content is a plain string (works on OpenAI / Anthropic / local)
    assert all(isinstance(m["content"], str) for m in msgs)


def test_native_image_blocks_ride_the_anchor_turn():
    blocks = [{"type": "input_image", "image_url": "data:image/png;base64,QUJD"}]

    # First hop: the plan prompt becomes a multimodal user turn.
    msgs = _build_transcript_messages(
        _FakeAssistant(), "q", {"_attachment_image_blocks": blocks}, "PLAN_PROMPT"
    )
    assert msgs == [{
        "role": "user",
        "content": [{"type": "input_text", "text": "PLAN_PROMPT"}, blocks[0]],
    }]

    # Later hops: only the anchor is multimodal (the transcript is rebuilt
    # statelessly per hop); tool/observation turns stay plain strings.
    payload = {
        "_attachment_image_blocks": blocks,
        "_acc_tool_calls": [{"tool": "text_search", "args": {"q": "x"}}],
        "_acc_tool_results": [{"tool": "text_search", "status": "ok", "summary": "found-it"}],
    }
    msgs = _build_transcript_messages(_FakeAssistant(), "q", payload, "PLAN_PROMPT")
    assert msgs[0]["content"][0] == {"type": "input_text", "text": "ANCHOR:q"}
    assert msgs[0]["content"][1]["type"] == "input_image"
    assert all(isinstance(m["content"], str) for m in msgs[1:])
