from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from component.logging import get_logger


log = get_logger(__name__)


def _tool_call_id(tc: Dict[str, Any]) -> Optional[int]:
    log.debugx(
        "Tool call id ophalen gestart",
        keys=list(tc.keys()) if isinstance(tc, dict) else None,
        raw_value=tc.get("tool_id") if isinstance(tc, dict) else None,
    )
    value = tc.get("tool_id")
    if value is None:
        log.debugx("Tool call id ontbreekt")
        return None
    try:
        result = int(value)
        log.debugx(
            "Tool call id geconverteerd",
            raw_value=value,
            tool_id=result,
        )
        return result
    except (TypeError, ValueError):
        log.warningx(
            "Tool call id kon niet worden geconverteerd",
            raw_value=value,
            raw_type=type(value).__name__,
        )
        return None


def _preview(obj: Any, max_chars: int = 1200) -> Any:
    log.debugx(
        "Preview maken gestart",
        object_type=type(obj).__name__,
        max_chars=max_chars,
    )
    try:
        s = json.dumps(obj, ensure_ascii=False)
        result = s[:max_chars] + ("..." if len(s) > max_chars else "")
        log.debugx(
            "Preview maken afgerond via json.dumps",
            original_length=len(s),
            result_length=len(result),
            truncated=len(s) > max_chars,
        )
        return result
    except Exception:
        s = str(obj)
        result = s[:max_chars] + ("..." if len(s) > max_chars else "")
        log.debugx(
            "Preview maken afgerond via str fallback",
            original_length=len(s),
            result_length=len(result),
            truncated=len(s) > max_chars,
        )
        return result


def _truncate_text(value: Any, max_chars: int = 4000) -> str:
    log.debugx(
        "Tekst afkappen gestart",
        value_type=type(value).__name__,
        max_chars=max_chars,
    )
    s = (value or "")
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    result = s[:max_chars] + ("…" if len(s) > max_chars else "")
    log.debugx(
        "Tekst afkappen afgerond",
        original_length=len(s),
        result_length=len(result),
        truncated=len(s) > max_chars,
    )
    return result


def _assistant_name(assistant: Any) -> str:
    result = getattr(assistant, "name", type(assistant).__name__)
    log.debugx(
        "Assistant naam bepaald",
        assistant_type=type(assistant).__name__,
        assistant_name=result,
    )
    return result


def build_result(
    *,
    mode: str,
    answer: str,
    trace: List[dict],
    thread_id: Optional[str],
    tool_calls: Optional[List[Any]] = None,
    tool_results: Optional[List[Any]] = None,
    docs: Optional[List[Any]] = None,
    pending_action: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    log.infox(
        "Orchestrator resultaat bouwen gestart",
        mode=mode,
        thread_id=thread_id,
        answer_length=len(answer or ""),
        trace_count=len(trace or []),
        tool_call_count=len(tool_calls or []),
        tool_result_count=len(tool_results or []),
        doc_count=len(docs or []),
        has_pending_action=pending_action is not None,
        extra_keys=list(extra.keys()),
    )
    result = {
        "mode": mode,
        "answer": answer,
        "tool_calls": tool_calls or [],
        "tool_results": tool_results or [],
        "docs": docs or [],
        "trace": trace,
        "thread_id": thread_id,
        "pending_action": pending_action,
        **extra,
    }
    log.infox(
        "Orchestrator resultaat bouwen afgerond",
        mode=result.get("mode"),
        thread_id=result.get("thread_id"),
        result_keys=list(result.keys()),
        tool_call_count=len(result.get("tool_calls") or []),
        tool_result_count=len(result.get("tool_results") or []),
        doc_count=len(result.get("docs") or []),
        trace_count=len(result.get("trace") or []),
    )
    return result


def _compact_tool_call(tc: Dict[str, Any]) -> Dict[str, Any]:
    log.debugx(
        "Tool call compact maken gestart",
        keys=list(tc.keys()) if isinstance(tc, dict) else None,
        raw_tool_id=tc.get("tool_id") if isinstance(tc, dict) else None,
        tool=tc.get("tool") if isinstance(tc, dict) else None,
    )
    result = {
        "tool_id": _tool_call_id(tc),
        "tool": (tc.get("tool") or "").strip(),
        "kind": (tc.get("kind") or "").strip(),
        "args": tc.get("args") or {},
        "reason": tc.get("reason"),
    }
    log.debugx(
        "Tool call compact maken afgerond",
        tool_id=result.get("tool_id"),
        tool=result.get("tool"),
        kind=result.get("kind"),
        arg_keys=list(result.get("args").keys()) if isinstance(result.get("args"), dict) else None,
        has_reason=bool(result.get("reason")),
    )
    return result


# Cap for inline content kept in a compacted tool result. Generous enough to hold a normal
# document (so the agent can actually read it) but bounded so one observation can't blow up
# the prompt. Matches the normalizer's full-inline limit.
_COMPACT_CONTENT_MAX_CHARS = 30000


def _compact_tool_result(tr: Any, max_chars: int = 500) -> Any:
    log.debugx(
        "Tool result compact maken gestart",
        result_type=type(tr).__name__,
        max_chars=max_chars,
    )
    if isinstance(tr, dict):
        compact = {}
        for k in (
            "ok",
            "status",
            "job_id",
            "doc_id",
            "file_path",
            "path",
            "tool",
            "error",
            "error_type",
            "message",
            "exit_code",
            "stdout_preview",
            "stderr_preview",
            "recoverable",
            "source",
            "score",
            "embedding_id",
            "doc",
        ):
            if k in tr:
                compact[k] = tr.get(k)

        # Preserve the actual content the normalizer made available inline. Without this the
        # agent only ever saw a ~500-char preview and could never read a document/search
        # result across hops (it kept "re-inspecting" and gave up). Keep content_text / facts
        # for full-inline results, capped so a single observation can't blow up the prompt.
        inspection_level = tr.get("inspection_level")
        if inspection_level:
            compact["inspection_level"] = inspection_level
        if tr.get("full_content_available_to_llm"):
            compact["full_content_available_to_llm"] = True
        content_text = tr.get("content_text")
        kept_content = False
        if isinstance(content_text, str) and content_text.strip():
            compact["content_text"] = content_text[:_COMPACT_CONTENT_MAX_CHARS]
            kept_content = True
        if inspection_level == "full_inline" and isinstance(tr.get("facts"), (dict, list)):
            compact["facts"] = tr.get("facts")
            kept_content = True
        # Only add the lossy preview when we didn't keep the real content (avoids duplicate
        # bulk and keeps the metadata-only case informative).
        if not kept_content:
            compact["preview"] = _preview(tr, max_chars=max_chars)
        log.debugx(
            "Tool result compact maken afgerond voor dict",
            input_keys=list(tr.keys()),
            compact_keys=list(compact.keys()),
            has_error="error" in compact,
            ok=compact.get("ok"),
            kept_content=kept_content,
        )
        return compact

    if isinstance(tr, list):
        result = {
            "count": len(tr),
            "preview": _preview(tr[:2], max_chars=max_chars),
        }
        log.debugx(
            "Tool result compact maken afgerond voor lijst",
            count=len(tr),
            preview_length=len(result.get("preview") or ""),
        )
        return result

    result = _preview(tr, max_chars=max_chars)
    log.debugx(
        "Tool result compact maken afgerond via preview fallback",
        result_type=type(tr).__name__,
        preview_length=len(result or ""),
    )
    return result


def _compact_doc(d: Dict[str, Any], max_chars: int = 1200) -> Dict[str, Any]:
    log.debugx(
        "Document compact maken gestart",
        keys=list(d.keys()) if isinstance(d, dict) else None,
        kind=d.get("kind") if isinstance(d, dict) else None,
        path=d.get("path") if isinstance(d, dict) else None,
        doc_id=d.get("doc_id") if isinstance(d, dict) else None,
        max_chars=max_chars,
    )
    result = {
        "kind": d.get("kind"),
        "meta": d.get("meta"),
        "path": d.get("path"),
        "doc_id": d.get("doc_id"),
        "selected": d.get("selected"),
        "content_preview": _truncate_text(d.get("content") or "", max_chars=max_chars),
        "source_tool": d.get("source_tool"),
    }
    log.debugx(
        "Document compact maken afgerond",
        kind=result.get("kind"),
        path=result.get("path"),
        doc_id=result.get("doc_id"),
        source_tool=result.get("source_tool"),
        content_preview_length=len(result.get("content_preview") or ""),
    )
    return result


def _compact_step_result(result: Dict[str, Any]) -> Dict[str, Any]:
    log.debugx(
        "Step result compact maken gestart",
        keys=list(result.keys()) if isinstance(result, dict) else None,
        mode=result.get("mode") if isinstance(result, dict) else None,
        answer_length=len(result.get("answer") or "") if isinstance(result, dict) else None,
        has_downstream_handoff=bool(result.get("downstream_handoff")) if isinstance(result, dict) else None,
    )
    compact = {
        "mode": result.get("mode"),
        "answer": _truncate_text(result.get("answer") or "", max_chars=1200),
        "downstream_handoff": result.get("downstream_handoff"),
    }
    log.debugx(
        "Step result compact maken afgerond",
        mode=compact.get("mode"),
        answer_length=len(compact.get("answer") or ""),
        has_downstream_handoff=bool(compact.get("downstream_handoff")),
    )
    return compact


def _compact_step_history_entry(step_entry: Dict[str, Any]) -> Dict[str, Any]:
    log.debugx(
        "Step history entry compact maken gestart",
        keys=list(step_entry.keys()) if isinstance(step_entry, dict) else None,
        step=step_entry.get("step") if isinstance(step_entry, dict) else None,
        assistant=step_entry.get("assistant") if isinstance(step_entry, dict) else None,
        status=step_entry.get("status") if isinstance(step_entry, dict) else None,
    )
    handoff = step_entry.get("downstream_handoff") or {}
    result = {
        "step": step_entry.get("step"),
        "assistant": step_entry.get("assistant"),
        "status": step_entry.get("status"),
        "downstream_handoff": {
            "summary": handoff.get("summary"),
            "facts": handoff.get("facts") or {},
            "artifacts": handoff.get("artifacts") or [],
            "open_questions": handoff.get("open_questions") or [],
            "output_ref": handoff.get("output_ref"),
            "status": handoff.get("status", step_entry.get("status")),
        },
    }
    log.debugx(
        "Step history entry compact maken afgerond",
        step=result.get("step"),
        assistant=result.get("assistant"),
        status=result.get("status"),
        handoff_status=result.get("downstream_handoff", {}).get("status"),
        artifact_count=len(result.get("downstream_handoff", {}).get("artifacts") or []),
        open_question_count=len(result.get("downstream_handoff", {}).get("open_questions") or []),
    )
    return result


def _compact_router_history(executed_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    log.infox(
        "Router history compact maken gestart",
        executed_step_count=len(executed_steps or []),
    )
    result = [_compact_step_history_entry(s) for s in executed_steps]
    log.infox(
        "Router history compact maken afgerond",
        executed_step_count=len(executed_steps or []),
        compact_step_count=len(result),
    )
    return result

def _extract_downstream_handoff(plan: Dict[str, Any], answer: str, docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    log.infox(
        "Downstream handoff extraheren gestart",
        plan_keys=list(plan.keys()) if isinstance(plan, dict) else None,
        answer_length=len(answer or ""),
        doc_count=len(docs or []),
        has_handoff=isinstance(plan.get("downstream_handoff") if isinstance(plan, dict) else None, dict),
    )
    handoff = plan.get("downstream_handoff")

    if isinstance(handoff, dict):
        result = {
            "summary": handoff.get("summary") or _truncate_text(answer or "", max_chars=1200),
            "full_answer": handoff.get("full_answer"),
            "artifacts": handoff.get("artifacts") or [
                {
                    "type": d.get("kind") or "document",
                    "path": d.get("path"),
                    "doc_id": d.get("doc_id"),
                    "meta": d.get("meta"),
                    "selected": d.get("selected"),
                    "content_preview": _truncate_text(d.get("content") or "", max_chars=1200),
                }
                for d in docs[:5]
            ],
            "facts": handoff.get("facts") or {},
            "iterables": handoff.get("iterables") or {},
            "open_questions": handoff.get("open_questions") or [],
            "output_ref": handoff.get("output_ref"),
            "status": handoff.get("status") or "success",
        }

        log.infox(
            "Downstream handoff extraheren afgerond vanuit plan",
            summary_length=len(result.get("summary") or ""),
            has_full_answer=bool(result.get("full_answer")),
            artifact_count=len(result.get("artifacts") or []),
            fact_keys=list((result.get("facts") or {}).keys()) if isinstance(result.get("facts"), dict) else None,
            iterable_keys=list((result.get("iterables") or {}).keys()) if isinstance(result.get("iterables"), dict) else None,
            open_question_count=len(result.get("open_questions") or []),
            output_ref=result.get("output_ref"),
            status=result.get("status"),
        )
        return result

    result = {
        "summary": _truncate_text(answer or "", max_chars=1200),
        "full_answer": answer or "",
        "artifacts": [
            {
                "type": d.get("kind") or "document",
                "path": d.get("path"),
                "doc_id": d.get("doc_id"),
                "meta": d.get("meta"),
                "selected": d.get("selected"),
                "content_preview": _truncate_text(d.get("content") or "", max_chars=1200),
            }
            for d in docs[:5]
        ],
        "facts": {},
        "iterables": {},
        "open_questions": [],
        "output_ref": None,
        "status": "success",
    }

    log.infox(
        "Downstream handoff extraheren afgerond via fallback",
        summary_length=len(result.get("summary") or ""),
        full_answer_length=len(result.get("full_answer") or ""),
        artifact_count=len(result.get("artifacts") or []),
        status=result.get("status"),
    )
    return result


def _as_list(x: Any) -> List[Dict[str, Any]]:
    log.debugx(
        "Waarde naar lijst converteren gestart",
        value_type=type(x).__name__,
        is_none=x is None,
    )
    if x is None:
        log.debugx("Waarde naar lijst converteren afgerond: None naar lege lijst")
        return []
    if isinstance(x, list):
        result = [i for i in x if isinstance(i, dict)]
        log.debugx(
            "Waarde naar lijst converteren afgerond: lijst gefilterd",
            input_count=len(x),
            output_count=len(result),
            skipped_count=len(x) - len(result),
        )
        return result
    if isinstance(x, dict):
        log.debugx(
            "Waarde naar lijst converteren afgerond: dict naar singleton lijst",
            keys=list(x.keys()),
        )
        return [x]
    log.debugx(
        "Waarde naar lijst converteren afgerond: unsupported type naar lege lijst",
        value_type=type(x).__name__,
    )
    return []


def _extract_final_answer_if_json(text: str) -> Optional[str]:
    log.debugx(
        "Final answer uit JSON extraheren gestart",
        text_length=len(text or ""),
    )
    s = (text or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        log.debugx("Final answer extractie overgeslagen: tekst lijkt geen JSON object")
        return None
    try:
        obj = json.loads(s)
    except Exception:
        log.debugx(
            "Final answer extractie mislukt: JSON parse error",
            text_length=len(s),
        )
        return None
    fa = obj.get("final_answer")
    if isinstance(fa, str) and fa.strip():
        result = fa.strip()
        log.debugx(
            "Final answer uit JSON geëxtraheerd",
            final_answer_length=len(result),
            json_keys=list(obj.keys()) if isinstance(obj, dict) else None,
        )
        return result
    log.debugx(
        "Final answer niet gevonden in JSON",
        json_keys=list(obj.keys()) if isinstance(obj, dict) else None,
        final_answer_type=type(fa).__name__,
    )
    return None


def _looks_like_planner_json(text: str) -> bool:
    log.debugx(
        "Planner JSON herkenning gestart",
        text_length=len(text or ""),
    )
    s = (text or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        log.debugx("Planner JSON herkenning afgerond: geen JSON object vorm")
        return False
    keys = ['"tool_calls"', '"action"', '"response_mode"', '"search_keywords"', '"args"', '"tool"']
    result = any(k in s for k in keys)
    log.debugx(
        "Planner JSON herkenning afgerond",
        result=result,
        matched_keys=[k for k in keys if k in s],
    )
    return result


def _fallback_no_evidence_message() -> str:
    log.infox("Fallback bericht voor ontbrekend bewijs bouwen")
    result = (
        "I couldn’t find enough evidence in the search results to answer yet.\n\n"
        "Try a broader or more specific search, for example:\n"
        "- fabric ingestion pipeline\n"
        "- nielseniq ingestion\n"
        "- microsoft fabric lakehouse shortcut\n"
        "- PL_INGEST_UNZIP\n"
        "- nielsen_ingestion_log\n"
    )
    log.debugx(
        "Fallback bericht voor ontbrekend bewijs gebouwd",
        length=len(result),
    )
    return result


def _coerce_plan_to_dict(plan: Any) -> Dict[str, Any]:
    log.infox(
        "Plan naar dict coërcen gestart",
        plan_type=type(plan).__name__,
    )
    if isinstance(plan, dict):
        log.debugx(
            "Plan is al dict",
            keys=list(plan.keys()),
        )
        return plan

    if isinstance(plan, list):
        log.warningx(
            "Planner gaf lijst terug in plaats van object",
            list_length=len(plan),
            single_dict=len(plan) == 1 and isinstance(plan[0], dict),
        )
        # Some models wrap the plan in an array or emit several objects. Take the first
        # dict that looks like a plan (has "action"); otherwise the first dict at all.
        plan_like = next((x for x in plan if isinstance(x, dict) and "action" in x), None)
        if plan_like is None:
            plan_like = next((x for x in plan if isinstance(x, dict)), None)
        if plan_like is not None:
            log.debugx("Plan uit lijst geëxtraheerd", keys=list(plan_like.keys()))
            return plan_like

        result = {
            "action": "error",
            "final_answer": "",
            "response_mode": "synthesize_answer",
            "tool_calls": [],
            "_plan_error": f"Planner returned a list instead of an object: {type(plan).__name__}",
            "_plan_raw": plan,
        }
        log.warningx(
            "Plan coërcion gaf error-plan terug voor lijst",
            list_length=len(plan),
            result_keys=list(result.keys()),
        )
        return result

    result = {
        "action": "error",
        "final_answer": "",
        "response_mode": "synthesize_answer",
        "tool_calls": [],
        "_plan_error": f"Planner returned unsupported type: {type(plan).__name__}",
        "_plan_raw": plan,
    }
    log.warningx(
        "Plan coërcion gaf error-plan terug voor unsupported type",
        plan_type=type(plan).__name__,
        result_keys=list(result.keys()),
    )
    return result