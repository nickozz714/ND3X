from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger


log = get_logger(__name__)


class PendingStore:
    def __init__(self):
        log.debugx("PendingStore initialiseren")
        self._pending_by_thread: Dict[str, Dict[str, Any]] = {}
        log.debugx(
            "PendingStore geïnitialiseerd",
            pending_count=len(self._pending_by_thread),
        )

    def get(self, thread_id: Optional[str]) -> Optional[Dict[str, Any]]:
        log.debugx(
            "Pending actie ophalen gestart",
            thread_id=thread_id,
            has_thread_id=bool(thread_id),
        )
        if not thread_id:
            log.debugx("Pending actie ophalen overgeslagen: thread_id ontbreekt")
            return None
        result = self._pending_by_thread.get(str(thread_id))
        log.debugx(
            "Pending actie ophalen afgerond",
            thread_id=str(thread_id),
            found=result is not None,
            pending_type=result.get("type") if isinstance(result, dict) else None,
            pending_keys=list(result.keys()) if isinstance(result, dict) else None,
            pending_count=len(self._pending_by_thread),
        )
        return result

    def set(self, thread_id: Optional[str], pending: Dict[str, Any]) -> None:
        log.infox(
            "Pending actie opslaan gestart",
            thread_id=thread_id,
            has_thread_id=bool(thread_id),
            pending_type=pending.get("type") if isinstance(pending, dict) else None,
            pending_keys=list(pending.keys()) if isinstance(pending, dict) else None,
        )
        if not thread_id:
            log.debugx("Pending actie opslaan overgeslagen: thread_id ontbreekt")
            return
        self._pending_by_thread[str(thread_id)] = pending
        log.infox(
            "Pending actie opgeslagen",
            thread_id=str(thread_id),
            pending_type=pending.get("type") if isinstance(pending, dict) else None,
            pending_count=len(self._pending_by_thread),
        )

    def clear(self, thread_id: Optional[str]) -> None:
        log.infox(
            "Pending actie wissen gestart",
            thread_id=thread_id,
            has_thread_id=bool(thread_id),
        )
        if not thread_id:
            log.debugx("Pending actie wissen overgeslagen: thread_id ontbreekt")
            return
        existed = str(thread_id) in self._pending_by_thread
        self._pending_by_thread.pop(str(thread_id), None)
        log.infox(
            "Pending actie gewist",
            thread_id=str(thread_id),
            existed=existed,
            pending_count=len(self._pending_by_thread),
        )


def is_confirmation_text(user_text: str) -> bool:
    log.debugx(
        "Confirmatietekst controleren gestart",
        text_length=len(user_text or ""),
        text_preview=(user_text or "")[:80],
    )
    s = (user_text or "").strip().lower()
    if not s:
        log.debugx("Confirmatietekst controleren afgerond: lege tekst")
        return False
    result = s in {
        "yes",
        "y",
        "confirm",
        "confirmed",
        "do it",
        "ok",
        "okay",
        "sure",
        "proceed",
        "go ahead",
    } or s.startswith("confirm ")
    log.debugx(
        "Confirmatietekst controleren afgerond",
        normalized=s,
        result=result,
    )
    return result


def is_cancellation_text(user_text: str) -> bool:
    log.debugx(
        "Annuleringstekst controleren gestart",
        text_length=len(user_text or ""),
        text_preview=(user_text or "")[:80],
    )
    s = (user_text or "").strip().lower()
    if not s:
        log.debugx("Annuleringstekst controleren afgerond: lege tekst")
        return False
    result = (
        s in {"no", "n", "cancel", "stop", "never mind", "dont", "don't", "nope"}
        or s.startswith("no ")
        or s.startswith("no,")
        or s.startswith("cancel ")
    )
    log.debugx(
        "Annuleringstekst controleren afgerond",
        normalized=s,
        result=result,
    )
    return result


def _tool_call_id(tc: Dict[str, Any]) -> Optional[int]:
    log.debugx(
        "Tool call id ophalen gestart",
        tc_keys=list(tc.keys()) if isinstance(tc, dict) else None,
        raw_tool_id=tc.get("tool_id") if isinstance(tc, dict) else None,
    )
    value = tc.get("tool_id")
    if value is None:
        log.debugx("Tool call id ontbreekt")
        return None
    try:
        result = int(value)
        log.debugx(
            "Tool call id ophalen afgerond",
            raw_tool_id=value,
            tool_id=result,
        )
        return result
    except (TypeError, ValueError):
        log.warningx(
            "Tool call id ongeldig",
            raw_tool_id=value,
            raw_type=type(value).__name__,
        )
        return None


def _is_default_server_tool(tool_name: str) -> bool:
    log.debugx(
        "Default server tool controleren gestart",
        tool_name=tool_name,
    )
    result = tool_name in {"text_ingest", "ingest_status"}
    log.debugx(
        "Default server tool controleren afgerond",
        tool_name=tool_name,
        result=result,
    )
    return result


def build_mutation_confirmation_prompt(tool_calls: List[Dict[str, Any]]) -> str:
    log.infox(
        "Mutation confirmation prompt bouwen gestart",
        tool_call_count=len(tool_calls or []),
    )
    lines = ["I’m about to make a change to your indexed documents:"]

    for tc in tool_calls:
        tool = (tc.get("tool") or "").strip()
        tool_id = _tool_call_id(tc)
        args = tc.get("args") or {}

        log.debugx(
            "Mutation confirmation tool call verwerken",
            tool=tool,
            tool_id=tool_id,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
        )

        if not _is_default_server_tool(tool) and tool_id is None:
            log.warningx(
                "Mutation confirmation prompt bouwen mislukt: dynamic tool call zonder tool_id",
                tool=tool,
                arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            )
            raise ValueError(f"Planner returned a dynamic tool call without tool_id for tool={tool!r}.")

        if tool == "text_update":
            doc_id = args.get("doc_id")
            new_content = (args.get("new_content") or "")
            preview = new_content[:240].rstrip()
            if len(new_content) > 240:
                preview += "…"
            lines.append(f"- Update doc_id={doc_id} with new content (preview):\n  {preview}")
            log.infox(
                "Mutation confirmation text_update toegevoegd",
                doc_id=doc_id,
                new_content_length=len(new_content),
                preview_length=len(preview),
            )
        elif tool == "text_delete":
            doc_id = args.get("doc_id")
            delete_file = args.get("delete_file", True)
            lines.append(f"- Delete doc_id={doc_id} (delete_file={delete_file})")
            log.infox(
                "Mutation confirmation text_delete toegevoegd",
                doc_id=doc_id,
                delete_file=delete_file,
            )

    lines += ["", "Reply **yes** to confirm, or **no** to cancel."]
    result = "\n".join(lines)
    log.infox(
        "Mutation confirmation prompt bouwen afgerond",
        tool_call_count=len(tool_calls or []),
        line_count=len(lines),
        prompt_length=len(result),
    )
    return result


def build_confirmed_mutation_answer(
    tool_calls: List[Dict[str, Any]],
    tool_results: List[Any],
    docs_from_mutation: List[Dict[str, Any]],
) -> str:
    log.infox(
        "Confirmed mutation answer bouwen gestart",
        tool_call_count=len(tool_calls or []),
        tool_result_count=len(tool_results or []),
        docs_from_mutation_count=len(docs_from_mutation or []),
    )
    lines = ["✅ Confirmed and executed the requested change(s)."]
    for tc, tr in zip(tool_calls, tool_results):
        tool = (tc.get("tool") or "").strip()
        args = tc.get("args") or {}

        log.debugx(
            "Confirmed mutation resultaat verwerken",
            tool=tool,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            result_type=type(tr).__name__,
            result_keys=list(tr.keys()) if isinstance(tr, dict) else None,
        )

        if tool == "text_update":
            doc_id = args.get("doc_id")
            ok = tr.get("ok") if isinstance(tr, dict) else None
            lines.append(f"- text_update doc_id={doc_id} ok={ok}")
            log.infox(
                "Confirmed mutation text_update toegevoegd",
                doc_id=doc_id,
                ok=ok,
            )
        elif tool == "text_delete":
            doc_id = args.get("doc_id")
            ok = tr.get("ok") if isinstance(tr, dict) else None
            lines.append(f"- text_delete doc_id={doc_id} ok={ok}")
            log.infox(
                "Confirmed mutation text_delete toegevoegd",
                doc_id=doc_id,
                ok=ok,
            )

    if docs_from_mutation:
        d0 = docs_from_mutation[0]
        log.infox(
            "Updated document toevoegen aan confirmed mutation answer",
            doc_id=d0.get("doc_id") if isinstance(d0, dict) else None,
            path=d0.get("path") if isinstance(d0, dict) else None,
            meta=d0.get("meta") if isinstance(d0, dict) else None,
            content_length=len(d0.get("content") or "") if isinstance(d0, dict) else None,
        )
        lines += ["", "---", f"### Updated document: {d0.get('path') or d0.get('meta')}"]
        if d0.get("doc_id") is not None:
            lines.append(f"(doc_id={d0.get('doc_id')})")
        lines += ["", d0.get("content") or ""]
    else:
        log.debugx("Geen updated document toegevoegd aan confirmed mutation answer")

    result = "\n".join(lines)
    log.infox(
        "Confirmed mutation answer bouwen afgerond",
        line_count=len(lines),
        answer_length=len(result),
        docs_from_mutation_count=len(docs_from_mutation or []),
    )
    return result