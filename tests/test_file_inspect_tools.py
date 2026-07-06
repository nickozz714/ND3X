import asyncio
import json
import zipfile

from services.assistants.orchestration.tool_result_artifacts import ToolResultNormalizer
from services.builtin.tools.file_tools import archive_list, file_inspect, notebook_inspect


def _artifact_for(name: str, content: str):
    n = ToolResultNormalizer(thread_id="ti", run_id=name)
    out = n.normalize(tool_call={"tool": "x", "tool_id": 999}, raw_result={"content": content})
    return out["content_ref"]


def test_file_inspect_text_code_json_jsonl_csv_notebook_zip_binary_and_compactness(tmp_path):
    # text/code
    text_ref = _artifact_for("text", "import os\nfrom x import y\nSELECT * FROM foo;\n" * 2000)
    t = asyncio.run(file_inspect({"content_ref": text_ref, "max_chars": 500}))
    assert t["file_type"] in ("text", "code")
    assert "facts" in t and len(json.dumps(t)) < 20000

    # json
    n = ToolResultNormalizer(thread_id="ti", run_id="json")
    json_art = n._write_artifact_bytes(tool_call_id="0", tool="x", data=json.dumps({"a": 1, "b": [1, 2], "c": {"d": "x"}}).encode(), filename="data.json", mime_type="application/json", truncated_for_llm=False, inspection_level="artifact_only")
    j = asyncio.run(file_inspect({"content_ref": json_art["content_ref"]}))
    assert j["file_type"] == "json"
    assert "top_level_keys" in j["facts"]

    # jsonl
    jsonl_ref = _artifact_for("jsonl", '{"a":1}\n{"b":2}\nnot-json\n')
    # force extension for dispatch
    n2 = ToolResultNormalizer(thread_id="ti", run_id="jl")
    jl = n2._write_artifact_bytes(tool_call_id="1", tool="x", data=b'{"a":1}\n{"b":2}\nnot-json\n', filename="sample.jsonl", mime_type="application/json", truncated_for_llm=False, inspection_level="artifact_only")
    jl_out = asyncio.run(file_inspect({"content_ref": jl["content_ref"]}))
    assert jl_out["file_type"] == "jsonl"
    assert jl_out["facts"]["valid_sample_count"] >= 2

    # notebook
    nb = {
        "nbformat": 4,
        "cells": [
            {"cell_type": "markdown", "source": ["# title"]},
            {"cell_type": "code", "source": ["import pandas as pd\ndf = spark.read.table('sales')\ndf.write.saveAsTable('out_tbl')"]},
        ],
    }
    n3 = ToolResultNormalizer(thread_id="ti", run_id="nb")
    nb_art = n3._write_artifact_bytes(tool_call_id="2", tool="x", data=json.dumps(nb).encode(), filename="job.ipynb", mime_type="application/json", truncated_for_llm=False, inspection_level="artifact_only")
    nb_out = asyncio.run(notebook_inspect({"content_ref": nb_art["content_ref"], "max_cells": 5}))
    assert nb_out["file_type"] == "notebook"
    assert nb_out["facts"]["cell_count"] == 2

    # csv
    n4 = ToolResultNormalizer(thread_id="ti", run_id="csv")
    csv_art = n4._write_artifact_bytes(tool_call_id="3", tool="x", data=b"id,name\n1,a\n2,b\n", filename="sample.csv", mime_type="text/csv", truncated_for_llm=False, inspection_level="artifact_only")
    csv_out = asyncio.run(file_inspect({"content_ref": csv_art["content_ref"], "max_rows": 2}))
    assert csv_out["file_type"] == "csv"
    assert csv_out["facts"]["columns"] == ["id", "name"]

    # zip
    zpath = tmp_path / "a.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("one.txt", "a")
        z.writestr("two.txt", "b")
    n5 = ToolResultNormalizer(thread_id="ti", run_id="zip")
    zip_art = n5._write_artifact_bytes(tool_call_id="4", tool="x", data=zpath.read_bytes(), filename="x.zip", mime_type="application/zip", truncated_for_llm=False, inspection_level="artifact_only")
    zip_out = asyncio.run(archive_list({"content_ref": zip_art["content_ref"], "max_entries": 1}))
    assert zip_out["file_type"] == "archive"
    assert zip_out["facts"]["file_count"] == 2
    assert len(zip_out["sample"]["entries"]) == 1

    # unknown/binary
    n6 = ToolResultNormalizer(thread_id="ti", run_id="bin")
    bin_art = n6._write_artifact_bytes(tool_call_id="5", tool="x", data=b"\x00\x01\x02\x03", filename="blob.bin", mime_type="application/octet-stream", truncated_for_llm=False, inspection_level="artifact_only")
    bin_out = asyncio.run(file_inspect({"content_ref": bin_art["content_ref"]}))
    assert bin_out["status"] == "partial"


def test_file_inspect_path_traversal_rejected():
    try:
        asyncio.run(file_inspect({"local_path": "/etc/passwd"}))
        assert False
    except ValueError:
        assert True
