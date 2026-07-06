from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from component.logging import get_logger


log = get_logger(__name__)


class OrchestratorTracer:
    def __init__(self, audit_service):
        log.infox(
            "OrchestratorTracer initialiseren",
            has_audit_service=audit_service is not None,
            audit_service_type=type(audit_service).__name__,
        )
        self.audit = audit_service
        self._turn_counter_by_thread: Dict[str, int] = {}
        self._seq_by_thread_turn: Dict[str, int] = {}
        log.infox(
            "OrchestratorTracer geïnitialiseerd",
            tracked_threads=0,
            tracked_thread_turns=0,
        )

    def next_turn_id(self, thread_id: Optional[str]) -> int:
        log.debugx(
            "Volgende turn_id bepalen gestart",
            thread_id=thread_id,
            has_thread_id=bool(thread_id),
        )
        if not thread_id:
            log.debugx(
                "Volgende turn_id bepalen afgerond zonder thread_id",
                turn_id=0,
            )
            return 0
        tid = str(thread_id)
        self._turn_counter_by_thread[tid] = self._turn_counter_by_thread.get(tid, 0) + 1
        result = self._turn_counter_by_thread[tid]
        log.debugx(
            "Volgende turn_id bepaald",
            thread_id=tid,
            turn_id=result,
            tracked_threads=len(self._turn_counter_by_thread),
        )
        return result

    def next_seq(self, thread_id: Optional[str], turn_id: int) -> int:
        log.debugx(
            "Volgende trace sequence bepalen gestart",
            thread_id=thread_id,
            turn_id=turn_id,
            has_thread_id=bool(thread_id),
        )
        if not thread_id:
            log.debugx(
                "Volgende trace sequence bepalen afgerond zonder thread_id",
                seq=0,
            )
            return 0
        key = f"{thread_id}:{turn_id}"
        self._seq_by_thread_turn[key] = self._seq_by_thread_turn.get(key, 0) + 1
        result = self._seq_by_thread_turn[key]
        log.debugx(
            "Volgende trace sequence bepaald",
            thread_id=str(thread_id),
            turn_id=turn_id,
            seq=result,
            tracked_thread_turns=len(self._seq_by_thread_turn),
        )
        return result

    def trace(
        self,
        trace: List[dict],
        *,
        thread_id: Optional[str],
        turn_id: int,
        type: str,
        level: str = "info",
        summary: str = "",
        data: Optional[Dict[str, Any]] = None,
        progress_cb: Optional[Any] = None,
    ) -> None:
        log.debugx(
            "Trace event aanmaken gestart",
            thread_id=thread_id,
            turn_id=turn_id,
            type=type,
            level=level,
            summary=summary,
            data_keys=list((data or {}).keys()),
            trace_count_before=len(trace or []),
            has_progress_cb=progress_cb is not None,
        )

        ts = time.time()
        seq = self.next_seq(thread_id, turn_id)

        event = {
            "ts": ts,
            "turn_id": turn_id,
            "seq": seq,
            "type": type,
            "level": level,
            "summary": summary,
            **(data or {}),
        }
        trace.append(event)

        log.infox(
            "Trace event toegevoegd",
            thread_id=thread_id,
            turn_id=turn_id,
            seq=seq,
            type=type,
            level=level,
            summary=summary,
            trace_count_after=len(trace),
            event_keys=list(event.keys()),
        )

        if thread_id:
            try:
                log.debugx(
                    "Trace event naar audit schrijven gestart",
                    thread_id=str(thread_id),
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                    level=level,
                )
                self.audit.append_event(
                    thread_id=str(thread_id),
                    turn_id=int(turn_id),
                    seq=int(seq),
                    type=str(type),
                    level=str(level),
                    summary=str(summary or type),
                    data=event,
                    ts=ts,
                )
                log.debugx(
                    "Trace event naar audit geschreven",
                    thread_id=str(thread_id),
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                )
            except Exception:
                log.warningx(
                    "Trace event naar audit schrijven mislukt",
                    thread_id=str(thread_id),
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                    level=level,
                )

        if progress_cb:
            try:
                log.debugx(
                    "Trace progress callback aanroepen gestart",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                )
                progress_cb(event)
                log.debugx(
                    "Trace progress callback aangeroepen",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                )
            except Exception:
                log.warningx(
                    "Trace progress callback aanroepen mislukt",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    seq=seq,
                    type=type,
                )
