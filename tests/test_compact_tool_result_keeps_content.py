"""Compacted tool results (what the agent sees across hops) must keep the inline content —
not reduce a document/search result to a ~500-char preview, which made the agent unable to
read documents and give up."""
from __future__ import annotations

from services.assistants.orchestration.formatting import _compact_tool_result


def test_full_inline_text_content_is_preserved():
    big = "DECLARATION ROW; " * 1000
    r = {
        "ok": True, "tool": "text__get_file", "path": "x.md",
        "inspection_level": "full_inline", "full_content_available_to_llm": True,
        "content_text": big,
    }
    c = _compact_tool_result(r, max_chars=500)
    assert c["content_text"].startswith("DECLARATION ROW;")
    assert len(c["content_text"]) > 5000          # not truncated to the 500-char preview
    assert c["full_content_available_to_llm"] is True
    assert "preview" not in c                       # real content replaces the lossy preview


def test_full_inline_structured_facts_are_preserved():
    r = {
        "ok": True, "tool": "text__search", "inspection_level": "full_inline",
        "facts": {"results": [{"i": i, "amount": i * 10} for i in range(40)]},
    }
    c = _compact_tool_result(r)
    assert len(c["facts"]["results"]) == 40


def test_error_result_still_gets_a_preview():
    c = _compact_tool_result({"status": "error", "message": "boom"})
    assert "preview" in c and c["status"] == "error"
