"""Full-vs-light eval harness (TODO 1.3/1.5) — scoring, storage and baseline
comparison. The orchestrator itself is exercised elsewhere; here the scorer and
report plumbing are unit-tested with canned results."""
from __future__ import annotations

import asyncio
import json

import services.model_eval_service as evalsvc
from services.model_eval_service import EVAL_TASKS, _score_task, _summarize_pair


def _task(task_id: str):
    return next(t for t in EVAL_TASKS if t["id"] == task_id)


def test_greeting_scored_completed():
    result = {"mode": "final", "answer": "Hello!", "tool_calls": [], "trace": []}
    s = _score_task(_task("greeting"), result, 12.0)
    assert s["completed"] and s["json_valid"] and s["problems"] == []


def test_greeting_with_tools_flagged():
    result = {"mode": "final", "answer": "Hello!", "trace": [],
              "tool_calls": [{"tool": "web_search"}]}
    s = _score_task(_task("greeting"), result, 12.0)
    assert not s["completed"]
    assert any("unexpected tool calls" in p for p in s["problems"])


def test_shell_task_requires_correct_tool():
    ok = {"mode": "confirm_action", "answer": "Run this shell command?",
          "tool_calls": [{"tool": "system__shell_exec"}], "trace": []}
    s = _score_task(_task("shell_tool"), ok, 60.0)
    assert s["completed"]

    wrong = {"mode": "confirm_action", "answer": "Run?", "trace": [],
             "tool_calls": [{"tool": "agent__dispatch"}]}
    s2 = _score_task(_task("shell_tool"), wrong, 60.0)
    assert not s2["completed"]
    assert any("system__shell_exec" in p for p in s2["problems"])


def test_tool_from_earlier_hop_found_in_trace():
    # Multi-hop final: result.tool_calls is empty, but the tool ran in hop 1 —
    # the scorer must find it via the trace's tool_call events.
    result = {
        "mode": "final", "answer": "- doc1.md\n- doc2.md", "tool_calls": [],
        "trace": [
            {"type": "tool_call", "tool": "text__list_files"},
            {"type": "tool_result", "tool": "text__list_files"},
            {"type": "planner_call_end", "duration_s": 20.0},
        ],
    }
    s = _score_task(_task("doc_list"), result, 170.0)
    assert s["completed"], s["problems"]
    assert "text__list_files" in s["tool_calls"]


def test_trace_deviations_and_unparseable_counted():
    result = {
        "mode": "final", "answer": "hi", "tool_calls": [],
        "trace": [
            {"type": "planner_call_end", "duration_s": 30.0},
            {"type": "plan_validation_failed"},
            {"type": "tool_id_resolved"},
            {"type": "error", "error": "planner_unparseable"},
        ],
    }
    s = _score_task(_task("greeting"), result, 31.0)
    assert s["validation_failures"] == 1
    assert s["deviations"] == 2
    assert s["json_valid"] is False
    assert s["planner_avg_s"] == 30.0


def test_summarize_pair_rates():
    cases = [
        {"completed": True, "json_valid": True, "validation_failures": 0, "deviations": 0, "elapsed_s": 10.0},
        {"completed": False, "json_valid": False, "validation_failures": 2, "deviations": 3, "elapsed_s": 30.0},
    ]
    s = _summarize_pair(cases)
    assert s["completion_rate"] == 0.5
    assert s["json_valid_rate"] == 0.5
    assert s["validation_failures"] == 2
    assert s["deviations"] == 3
    assert s["avg_elapsed_s"] == 20.0


def test_run_eval_stores_report_and_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(evalsvc, "eval_dir", lambda: tmp_path)

    async def fake_case(model, mode, task):
        return {
            "task_id": task["id"], "completed": True, "json_valid": True,
            "problems": [], "mode": "final", "tool_calls": [],
            "answer_preview": "ok", "elapsed_s": 5.0, "planner_calls": 1,
            "unparseable": 0, "validation_failures": 0, "deviations": 0,
            "planner_avg_s": 5.0,
        }

    monkeypatch.setattr(evalsvc, "_run_case", fake_case)

    first = asyncio.run(evalsvc.run_eval(models=["m1"], modes=["light"], task_ids=["greeting"]))
    assert first["status"] == "completed"
    stored = json.loads((tmp_path / f"eval_{first['run_id']}.json").read_text())
    assert stored["results"]["m1|light"]["summary"]["completion_rate"] == 1.0
    assert "baseline" not in stored["results"]["m1|light"]

    # ensure a distinct run_id (timestamp-based) for the second run
    import time
    time.sleep(1.1)
    second = asyncio.run(evalsvc.run_eval(models=["m1"], modes=["light"], task_ids=["greeting"]))
    pair = second["results"]["m1|light"]
    assert pair["baseline"]["compared_to_run"] == first["run_id"]
    assert pair["baseline"]["completion_rate_delta"] == 0.0
