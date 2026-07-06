import asyncio
from pathlib import Path

from services.assistants.orchestration.tool_result_artifacts import ToolResultNormalizer
from services.builtin.tools.file_tools import file_preview, file_search_text


def test_small_text_inlined():
    n = ToolResultNormalizer(thread_id="t1", run_id="r1")
    out = n.normalize(tool_call={"tool": "x", "tool_id": 1}, raw_result={"content": "hello"})
    assert out["inspection_level"] == "full_inline"
    assert out["content_text"] == "hello"


def test_large_text_artifact_preview():
    n = ToolResultNormalizer(thread_id="t2", run_id="r2")
    txt = "a" * 40000
    out = n.normalize(tool_call={"tool": "x", "tool_id": 2}, raw_result={"content": txt})
    assert out["inspection_level"] == "preview_only"
    assert out["truncated"] is True


def test_structured_result_inlined_full_not_metadata_only():
    """A normal structured result (no text key) must be delivered FULL inline and marked
    trustworthy — previously it was metadata_only / truncated-to-artifact above 8k, so the
    agent couldn't read e.g. a 20k search result and gave up."""
    n = ToolResultNormalizer(thread_id="t3", run_id="r3")
    big_structured = {"results": [{"i": i, "text": "x" * 50} for i in range(300)]}  # ~>8k, <30k
    out = n.normalize(tool_call={"tool": "text__search", "tool_id": 297}, raw_result=big_structured)
    assert out["inspection_level"] == "full_inline"
    assert out["full_content_available_to_llm"] is True
    assert out["facts"] == big_structured


def test_huge_structured_result_still_artifact_preview():
    n = ToolResultNormalizer(thread_id="t4", run_id="r4")
    huge = {"results": [{"i": i, "text": "x" * 200} for i in range(1000)]}  # >30k
    out = n.normalize(tool_call={"tool": "text__search", "tool_id": 297}, raw_result=huge)
    assert out["inspection_level"] == "preview_only"
    assert out["truncated"] is True
    assert out["content_ref"]


def test_binary_base64_artifact_only():
    n = ToolResultNormalizer(thread_id="t3", run_id="r3")
    import base64
    data = base64.b64encode(b"\x00" * 512).decode()
    out = n.normalize(tool_call={"tool": "x", "tool_id": 3}, raw_result={"file_bytes": data})
    assert out["inspection_level"] == "artifact_only"
    assert out["content_ref"]


def test_file_tools_and_path_traversal():
    n = ToolResultNormalizer(thread_id="t4", run_id="r4")
    out = n.normalize(tool_call={"tool": "x", "tool_id": 4}, raw_result={"content": "needle\nabc needle xyz" * 3000})
    preview = asyncio.run(file_preview({"content_ref": out["content_ref"], "max_chars": 50}))
    assert preview["status"] == "success"
    matches = asyncio.run(file_search_text({"content_ref": out["content_ref"], "query": "needle", "max_matches": 2}))
    assert matches["match_count"] <= 2
    try:
        asyncio.run(file_preview({"local_path": "/etc/passwd"}))
        assert False, "expected error"
    except ValueError:
        assert True
