# services/voice_live_service.py
from __future__ import annotations

import asyncio
import difflib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from openai import OpenAI

from services.openai_service import OpenAIResponsesService, ResponseResult
from component.logging import get_logger, log_context
from services.voice.voice_profiles.registry import get_profile

from services.voice.voice_utilities import (
    safe_slug,
    utc_iso,
    fmt_timerange,
    ffprobe_duration_s,
    webm_file_segment_to_wav_bytes,
    wav_duration_s,
    write_json,
    read_json,
    write_text,
    append_jsonl,
    read_jsonl,
    touch_marker,
    atomic_write_json,
)

log = get_logger("svc.voice_live")


def _delta_from_windows(prev: str, curr: str, *, min_new_words: int = 4) -> str:
    """
    Robust delta between two sliding-window ASR texts using word-level diff.
    Prevents 'freeze' when punctuation changes or ASR jitter occurs.
    """
    log.debugx(
        "voice_live:delta_from_windows:start",
        prev_len=len(prev or ""),
        curr_len=len(curr or ""),
        min_new_words=min_new_words,
    )

    prev = (prev or "").strip()
    curr = (curr or "").strip()

    if not curr:
        log.debugx("voice_live:delta_from_windows:empty_curr")
        return ""

    if not prev:
        log.debugx(
            "voice_live:delta_from_windows:no_prev_return_curr",
            curr_len=len(curr),
        )
        return curr

    prev_words = prev.split()
    curr_words = curr.split()

    log.debugx(
        "voice_live:delta_from_windows:words",
        prev_word_count=len(prev_words),
        curr_word_count=len(curr_words),
    )

    if not prev_words or not curr_words:
        result = curr if curr != prev else ""
        log.debugx(
            "voice_live:delta_from_windows:word_fallback",
            result_len=len(result),
            changed=curr != prev,
        )
        return result

    sm = difflib.SequenceMatcher(a=prev_words, b=curr_words)
    blocks = sm.get_matching_blocks()
    best = max(blocks, key=lambda b: (b.size, b.b + b.size), default=None)

    if not best or best.size == 0:
        log.debugx(
            "voice_live:delta_from_windows:no_match_return_curr",
            curr_len=len(curr),
        )
        return curr  # better to append than freeze

    start = best.b + best.size
    tail = curr_words[start:]

    log.debugx(
        "voice_live:delta_from_windows:best_match",
        best_a=best.a,
        best_b=best.b,
        best_size=best.size,
        tail_word_count=len(tail),
    )

    if len(tail) >= min_new_words:
        result = " ".join(tail).strip()
        log.debugx(
            "voice_live:delta_from_windows:return_tail",
            result_len=len(result),
            tail_word_count=len(tail),
        )
        return result

    ratio = difflib.SequenceMatcher(a=prev, b=curr).ratio()
    result = curr if ratio < 0.60 else ""

    log.debugx(
        "voice_live:delta_from_windows:ratio_fallback",
        ratio=ratio,
        result_len=len(result),
    )
    return result


@dataclass
class VoiceLiveResult:
    thread_id: str
    run_id: str
    run_dir: str
    markdown: str
    data: Dict[str, Any]


class VoiceLiveService:
    """
    Live pipeline.

    Mechanics unchanged:
      - recording.webm append
      - tail extraction with ffprobe + -ss AFTER -i
      - sliding window ASR -> robust delta -> append jsonl
      - periodic updater consumes jsonl delta -> state.json -> markdown.md

    Changes:
      - uses shared utilities (voice_utilities)
      - uses shared markdown renderer service (services.markdown.renderer)
    """

    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_TRANSCRIBING = "transcribing"
    STATE_UPDATING = "updating"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(
        self,
        responses: OpenAIResponsesService,
        *,
        client: Optional[OpenAI] = None,
        # None => resolve from the OpenAI-backed 'transcription' slot at call time
        # (no hardcoded default). An explicit value still overrides the slot.
        transcription_model: Optional[str] = None,
        keep_context: bool = True,
        voice_root: str | Path = "voice",
        update_every_s: int = 15,
        max_chunk_bytes: int = 20 * 1024 * 1024,
        # transcription cadence
        transcribe_every_chunks: int = 3,   # if timeslice=10s, ASR ~every 30s
        transcribe_tail_s: int = 45,
        probe_first_s: int = 20,
        min_wav_s: float = 0.11,
        min_text_len: int = 2,
    ):
        log.infox(
            "voice_live:init:start",
            has_responses=responses is not None,
            client_provided=client is not None,
            transcription_model=transcription_model,
            keep_context=keep_context,
            voice_root=str(voice_root),
            update_every_s=update_every_s,
            max_chunk_bytes=max_chunk_bytes,
            transcribe_every_chunks=transcribe_every_chunks,
            transcribe_tail_s=transcribe_tail_s,
            probe_first_s=probe_first_s,
            min_wav_s=min_wav_s,
            min_text_len=min_text_len,
        )

        self.responses = responses
        # Resolve the OpenAI SDK client lazily (see the `client` property): voice
        # is the only consumer, so a missing OpenAI key must never block startup.
        self._client = client
        self.transcription_model = transcription_model
        self.keep_context = bool(keep_context)
        self.voice_root = Path(voice_root)
        self.voice_root.mkdir(parents=True, exist_ok=True)

        self.update_every_s = int(max(5, update_every_s))
        self.max_chunk_bytes = int(max(1_000_000, max_chunk_bytes))

        self.transcribe_every_chunks = int(max(1, transcribe_every_chunks))
        self.transcribe_tail_s = int(max(10, transcribe_tail_s))
        self.probe_first_s = int(max(3, probe_first_s))
        self.min_wav_s = float(max(0.1, min_wav_s))
        self.min_text_len = int(max(0, min_text_len))

        self._run_locks: Dict[str, asyncio.Lock] = {}
        self._run_update_tasks: Dict[str, asyncio.Task] = {}
        # Strong refs to detached meeting-action tasks (#9) so they aren't GC'd
        # mid-flight; discarded on completion.
        self._action_tasks: Set[asyncio.Task] = set()

        self._run_has_recording_header: Set[str] = set()
        self._run_last_window_text: Dict[str, str] = {}

        log.infox(
            "voice_live:init:done",
            voice_root=str(self.voice_root),
            voice_root_exists=self.voice_root.exists(),
            update_every_s=self.update_every_s,
            max_chunk_bytes=self.max_chunk_bytes,
            transcribe_every_chunks=self.transcribe_every_chunks,
            transcribe_tail_s=self.transcribe_tail_s,
            probe_first_s=self.probe_first_s,
            min_wav_s=self.min_wav_s,
            min_text_len=self.min_text_len,
            has_client=self._client is not None,
        )

    @property
    def client(self):
        """OpenAI SDK client, built on first use. Deferred so the app boots
        without an OpenAI key; only the voice feature actually needs it."""
        if self._client is None:
            self._client = self.responses.client
        return self._client

    # ---------------------------------------------------------------------
    # Slot resolution (no hardcoded model defaults)
    # ---------------------------------------------------------------------
    def _resolve_transcription_model(self, explicit: Optional[str] = None) -> str:
        """Effective transcription model: explicit value, else the OpenAI-backed
        'transcription' slot. Raises when nothing is configured so the
        OpenAI-only live transcription gates off cleanly (no hardcoded model)."""
        if explicit:
            return explicit
        from db.database import SessionLocal
        from services.providers.provider_factory import resolve_openai_transcription_model
        db = SessionLocal()
        try:
            mid = resolve_openai_transcription_model(db)
        finally:
            db.close()
        if not mid:
            raise RuntimeError(
                "Live transcription is not configured. Assign an OpenAI transcription "
                "model to the Recordings (STT) slot under AI Models → Routing."
            )
        return mid

    @staticmethod
    def _resolve_chat_model() -> Optional[str]:
        """First assigned chat-slot model for the live notes assistant. None when
        no chat slot is configured."""
        from db.database import SessionLocal
        from services.providers.provider_factory import resolve_default_chat_model
        db = SessionLocal()
        try:
            return resolve_default_chat_model(db)
        except Exception as exc:  # noqa: BLE001 — never break the voice path
            log.warningx("voice_live:resolve_chat_model:failed", error=str(exc))
            return None
        finally:
            db.close()

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    async def start_live_job(
        self,
        *,
        thread_id: str,
        model: str,
        payload: Optional[Dict[str, Any]] = None,
        original_filename: str = "live.webm",
        content_type: str = "audio/webm",
    ) -> Dict[str, Any]:
        log.infox(
            "voice_live:start_job:start",
            thread_id=thread_id,
            model=model,
            original_filename=original_filename,
            content_type=content_type,
            payload_keys=list((payload or {}).keys()),
        )

        payload = payload or {}
        profile = get_profile((payload or {}).get("profile"))

        log.debugx(
            "voice_live:start_job:profile_selected",
            thread_id=thread_id,
            profile_id=getattr(profile, "id", None),
        )

        run_dir = self._make_run_dir(thread_id=thread_id, kind="live", original_filename=original_filename)
        run_id = run_dir.name

        key = str(run_dir)
        self._run_has_recording_header.discard(key)
        self._run_last_window_text.pop(key, None)

        log.debugx(
            "voice_live:start_job:runtime_state_reset",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
            key=key,
        )

        client_timeslice_s = float(payload.get("client_timeslice_s") or 10.0)

        meta = {
            "kind": "live",
            "thread_id": thread_id,
            "run_id": run_id,
            "created_utc": utc_iso(),
            "content_type": content_type,
            "original_filename": original_filename,
            "assistant_model": model,
            "payload": payload,
            "client_timeslice_s": client_timeslice_s,
            "transcription_model": self.transcription_model,
            "update_every_s": self.update_every_s,
            "transcribe_every_chunks": self.transcribe_every_chunks,
            "transcribe_tail_s": self.transcribe_tail_s,
            "probe_first_s": self.probe_first_s,
        }

        log.debugx(
            "voice_live:start_job:write_meta",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
            meta_keys=list(meta.keys()),
            client_timeslice_s=client_timeslice_s,
        )
        write_json(run_dir / "meta.json", meta)

        # store selected profile in meta for later steps
        payload["profile"] = profile.id

        state = profile.empty_state()

        log.debugx(
            "voice_live:start_job:write_initial_artifacts",
            thread_id=thread_id,
            run_id=run_id,
            state_keys=list(state.keys()) if isinstance(state, dict) else None,
        )
        write_json(run_dir / "state.json", state)
        write_text(run_dir / "markdown.md", profile.render(state))
        write_text(run_dir / "live_transcript.jsonl", "")

        (run_dir / "recording.webm").write_bytes(b"")

        log.debugx(
            "voice_live:start_job:recording_initialized",
            thread_id=thread_id,
            run_id=run_id,
            recording_path=str(run_dir / "recording.webm"),
        )

        self._write_status(
            run_dir,
            state=self.STATE_QUEUED,
            step="created",
            progress=0.0,
            message="Live job created.",
            error=None,
        )

        self._ensure_periodic_updater(run_dir=run_dir)

        result = {"thread_id": thread_id, "run_id": run_id, "run_dir": str(run_dir)}

        log.infox(
            "voice_live:start_job:done",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
            profile_id=getattr(profile, "id", None),
        )
        return result

    async def ingest_live_chunk(
        self,
        *,
        thread_id: str,
        run_id: str,
        chunk_index: int,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> Dict[str, Any]:
        log.infox(
            "voice_live:ingest_chunk:start",
            thread_id=thread_id,
            run_id=run_id,
            chunk_index=chunk_index,
            filename=filename,
            content_type=content_type,
            bytes_count=len(audio_bytes or b""),
        )

        run_dir = self._resolve_run_dir(thread_id=thread_id, run_id=run_id)
        meta = read_json(run_dir / "meta.json") or {}

        log.debugx(
            "voice_live:ingest_chunk:meta_loaded",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
            meta_keys=list(meta.keys()) if isinstance(meta, dict) else None,
        )

        if not audio_bytes:
            log.warningx(
                "voice_live:ingest_chunk:empty_chunk",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
            )
            raise ValueError("Empty chunk upload.")

        if len(audio_bytes) > self.max_chunk_bytes:
            log.warningx(
                "voice_live:ingest_chunk:chunk_too_large",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                bytes_count=len(audio_bytes),
                max_chunk_bytes=self.max_chunk_bytes,
            )
            raise ValueError(
                f"Chunk too large ({len(audio_bytes)} bytes). Increase max_chunk_bytes or reduce timeslice."
            )

        lock = self._get_lock(str(run_dir))
        async with lock:
            log.debugx(
                "voice_live:ingest_chunk:lock_acquired",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                run_dir=str(run_dir),
            )

            chunks_dir = run_dir / "chunks"
            chunks_dir.mkdir(parents=True, exist_ok=True)

            chunk_path = chunks_dir / f"chunk_{int(chunk_index):06d}{Path(filename).suffix or '.webm'}"
            chunk_path.write_bytes(audio_bytes)

            log.debugx(
                "voice_live:ingest_chunk:chunk_written",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                chunk_path=str(chunk_path),
                bytes_count=len(audio_bytes),
            )

            key = str(run_dir)
            recording_path = run_dir / "recording.webm"

            # Build the full recording safely
            if key not in self._run_has_recording_header:
                recording_path.write_bytes(audio_bytes)
                self._run_has_recording_header.add(key)
                log.debugx(
                    "voice_live:ingest_chunk:recording_header_initialized",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=chunk_index,
                    recording_path=str(recording_path),
                    recording_bytes=recording_path.stat().st_size if recording_path.exists() else 0,
                )
            else:
                ebml = b"\x1A\x45\xDF\xA3"
                if audio_bytes.startswith(ebml):
                    cluster = b"\x1F\x43\xB6\x75"
                    i = audio_bytes.find(cluster)
                    if i > 0:
                        log.debugx(
                            "voice_live:ingest_chunk:ebml_header_trimmed",
                            thread_id=thread_id,
                            run_id=run_id,
                            chunk_index=chunk_index,
                            cluster_offset=i,
                            original_bytes=len(audio_bytes),
                        )
                        audio_bytes = audio_bytes[i:]
                with recording_path.open("ab") as f:
                    f.write(audio_bytes)

                log.debugx(
                    "voice_live:ingest_chunk:recording_appended",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=chunk_index,
                    appended_bytes=len(audio_bytes),
                    recording_bytes=recording_path.stat().st_size if recording_path.exists() else 0,
                )

            self._write_status(
                run_dir,
                state=self.STATE_TRANSCRIBING,
                step=f"chunk_{int(chunk_index):06d}",
                progress=self._rough_progress(run_dir),
                message=f"Received chunk {chunk_index}.",
                error=None,
            )

            do_asr = (chunk_index > 0) and (chunk_index % self.transcribe_every_chunks == 0)

            log.debugx(
                "voice_live:ingest_chunk:asr_decision",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                do_asr=bool(do_asr),
                transcribe_every_chunks=self.transcribe_every_chunks,
            )

            window_text = ""
            delta_text = ""
            elapsed = 0.0
            method_used = ""
            wav_dur = 0.0

            if do_asr:
                log.infox(
                    "voice_live:ingest_chunk:asr_start",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=chunk_index,
                )
                t0 = asyncio.get_running_loop().time()
                window_text, method_used, wav_dur = await self._transcribe_live_window_async(run_dir)
                elapsed = round(asyncio.get_running_loop().time() - t0, 2)

                prev = self._run_last_window_text.get(key, "")
                delta_text = _delta_from_windows(prev, window_text, min_new_words=4)
                self._run_last_window_text[key] = window_text or prev

                log.infox(
                    "voice_live:ingest_chunk:asr_done",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=chunk_index,
                    elapsed_s=elapsed,
                    method=method_used,
                    wav_dur_s=wav_dur,
                    window_text_len=len(window_text),
                    prev_window_text_len=len(prev),
                    delta_text_len=len(delta_text),
                )

            asr_obj = {
                "chunk_index": int(chunk_index),
                "created_utc": utc_iso(),
                "filename": chunk_path.name,
                "recording_bytes": recording_path.stat().st_size if recording_path.exists() else 0,
                "elapsed_s": elapsed,
                "asr": {
                    "did_transcribe": bool(do_asr),
                    "window_text_len": len(window_text),
                    "delta_text_len": len(delta_text),
                    "method": method_used,
                    "wav_dur_s": wav_dur,
                    "tail_s": self.transcribe_tail_s,
                },
                "text": delta_text,
            }
            write_json(chunks_dir / f"asr_{int(chunk_index):06d}.json", asr_obj)

            log.debugx(
                "voice_live:ingest_chunk:asr_artifact_written",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                asr_path=str(chunks_dir / f"asr_{int(chunk_index):06d}.json"),
                did_transcribe=bool(do_asr),
            )

            timeslice_s = float(
                meta.get("client_timeslice_s")
                or (meta.get("payload") or {}).get("client_timeslice_s")
                or 10.0
            )
            approx_start_s = float(chunk_index) * timeslice_s
            approx_end_s = approx_start_s + timeslice_s

            log.debugx(
                "voice_live:ingest_chunk:timerange_estimated",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                timeslice_s=timeslice_s,
                approx_start_s=approx_start_s,
                approx_end_s=approx_end_s,
            )

            if delta_text and len(delta_text) >= self.min_text_len:
                append_jsonl(
                    run_dir / "live_transcript.jsonl",
                    {
                        "chunk_index": int(chunk_index),
                        "start_s": approx_start_s,
                        "end_s": approx_end_s,
                        "text": delta_text,
                        "utc": utc_iso(),
                        "note": f"delta_tail_{self.transcribe_tail_s}s:{method_used}",
                    },
                )
                touch_marker(run_dir / ".new_transcript")
                log.infox(
                    "voice_live:asr_appended",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=int(chunk_index),
                    text_len=len(delta_text),
                    method=method_used,
                    marker=str(run_dir / ".new_transcript"),
                )
            else:
                log.infox(
                    "voice_live:asr_empty_or_skipped",
                    thread_id=thread_id,
                    run_id=run_id,
                    chunk_index=int(chunk_index),
                    did_transcribe=bool(do_asr),
                    delta_text_len=len(delta_text),
                    min_text_len=self.min_text_len,
                )

            result = {"ok": True, "chunk_index": int(chunk_index), "did_transcribe": bool(do_asr), "text_len": len(delta_text)}

            log.infox(
                "voice_live:ingest_chunk:done",
                thread_id=thread_id,
                run_id=run_id,
                chunk_index=chunk_index,
                did_transcribe=bool(do_asr),
                text_len=len(delta_text),
            )
            return result

    async def stop_live_job(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        log.infox(
            "voice_live:stop_job:start",
            thread_id=thread_id,
            run_id=run_id,
        )

        run_dir = self._resolve_run_dir(thread_id=thread_id, run_id=run_id)
        lock = self._get_lock(str(run_dir))
        async with lock:
            log.debugx(
                "voice_live:stop_job:lock_acquired",
                thread_id=thread_id,
                run_id=run_id,
                run_dir=str(run_dir),
            )

            # Final window pass (best effort)
            try:
                log.infox(
                    "voice_live:stop_job:final_window_start",
                    thread_id=thread_id,
                    run_id=run_id,
                )
                window_text, method_used, wav_dur = await self._transcribe_live_window_async(run_dir)
                prev = self._run_last_window_text.get(str(run_dir), "")
                delta = _delta_from_windows(prev, window_text, min_new_words=2)
                if not delta:
                    delta = window_text

                log.infox(
                    "voice_live:stop_job:final_window_done",
                    thread_id=thread_id,
                    run_id=run_id,
                    method=method_used,
                    wav_dur_s=wav_dur,
                    window_text_len=len(window_text),
                    prev_len=len(prev),
                    delta_len=len(delta or ""),
                )

                if delta and delta.strip():
                    append_jsonl(
                        run_dir / "live_transcript.jsonl",
                        {
                            "chunk_index": 10_000_000,
                            "start_s": None,
                            "end_s": None,
                            "text": delta,
                            "utc": utc_iso(),
                            "note": f"final_delta_window:{method_used}:wav={wav_dur:.2f}",
                        },
                    )
                    touch_marker(run_dir / ".new_transcript")
                    log.infox(
                        "voice_live:stop_job:final_delta_appended",
                        thread_id=thread_id,
                        run_id=run_id,
                        delta_len=len(delta),
                    )
            except Exception:
                log.exception("voice_live:final_window_failed")

            log.infox(
                "voice_live:stop_job:update_state_start",
                thread_id=thread_id,
                run_id=run_id,
            )
            await self._update_state_from_new_transcript(run_dir)
            log.infox(
                "voice_live:stop_job:update_state_done",
                thread_id=thread_id,
                run_id=run_id,
            )

            try:
                log.infox(
                    "voice_live:stop_job:final_cleanup_start",
                    thread_id=thread_id,
                    run_id=run_id,
                )
                await self._final_cleanup_markdown(run_dir)
                log.infox(
                    "voice_live:stop_job:final_cleanup_done",
                    thread_id=thread_id,
                    run_id=run_id,
                )
            except Exception as e:
                log.errorx("voice_live:final_cleanup_failed", exception=e)

            self._write_status(
                run_dir,
                state=self.STATE_DONE,
                step="stopped",
                progress=1.0,
                message="Live job stopped.",
                error=None,
            )

            log.infox(
                "voice_live:stop_job:done",
                thread_id=thread_id,
                run_id=run_id,
                run_dir=str(run_dir),
            )
            return {"ok": True}

    def get_live_status(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        log.infox(
            "voice_live:get_status:start",
            thread_id=thread_id,
            run_id=run_id,
        )

        run_dir = self._resolve_run_dir(thread_id=thread_id, run_id=run_id)
        status = read_json(run_dir / "status.json")
        if isinstance(status, dict):
            status["artifacts"] = self._artifact_paths(run_dir)
            log.infox(
                "voice_live:get_status:found",
                thread_id=thread_id,
                run_id=run_id,
                state=status.get("state"),
                step=status.get("step"),
                progress=status.get("progress"),
                artifact_count=len(status.get("artifacts") or {}),
            )
            return status

        log.warningx(
            "voice_live:get_status:not_found",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
        )
        return {
            "state": "unknown",
            "step": "",
            "progress": 0.0,
            "message": "No status found.",
            "created_utc": None,
            "updated_utc": utc_iso(),
            "error": None,
            "artifacts": self._artifact_paths(run_dir),
        }

    def get_live_result(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        log.infox(
            "voice_live:get_result:start",
            thread_id=thread_id,
            run_id=run_id,
        )

        run_dir = self._resolve_run_dir(thread_id=thread_id, run_id=run_id)
        out: Dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "status": self.get_live_status(thread_id=thread_id, run_id=run_id),
            "artifacts": self._artifact_paths(run_dir),
        }
        st = read_json(run_dir / "state.json")
        if isinstance(st, dict):
            out["data"] = st
        md = run_dir / "markdown.md"
        if md.exists():
            out["markdown"] = md.read_text(encoding="utf-8", errors="replace")
        # Meeting-driven action cards (#9), if any were produced this run.
        try:
            from services.voice.meeting_action_service import read_actions
            out["actions"] = read_actions(run_dir)
        except Exception:  # noqa: BLE001 — actions are best-effort, never block the result
            out["actions"] = []

        log.infox(
            "voice_live:get_result:done",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
            has_data="data" in out,
            has_markdown="markdown" in out,
            markdown_len=len(out.get("markdown") or ""),
            artifact_count=len(out.get("artifacts") or {}),
            status_state=(out.get("status") or {}).get("state") if isinstance(out.get("status"), dict) else None,
        )
        return out

    def get_live_actions(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        """Meeting-driven action cards (#9) produced so far for this run."""
        run_dir = self._resolve_run_dir(thread_id=thread_id, run_id=run_id)
        try:
            from services.voice.meeting_action_service import read_actions
            actions = read_actions(run_dir)
        except Exception:  # noqa: BLE001
            actions = []
        return {"thread_id": thread_id, "run_id": run_id, "actions": actions}

    # ---------------------------------------------------------------------
    # Periodic updater
    # ---------------------------------------------------------------------
    def _ensure_periodic_updater(self, *, run_dir: Path) -> None:
        key = str(run_dir)

        log.debugx(
            "voice_live:ensure_updater:start",
            run_dir=str(run_dir),
            key=key,
            existing_task=key in self._run_update_tasks,
            existing_task_done=self._run_update_tasks[key].done() if key in self._run_update_tasks else None,
        )

        if key in self._run_update_tasks and not self._run_update_tasks[key].done():
            log.debugx(
                "voice_live:ensure_updater:already_running",
                run_dir=str(run_dir),
                key=key,
            )
            return

        async def _loop():
            meta = read_json(run_dir / "meta.json") or {}
            thread_id = meta.get("thread_id") or "unknown"
            assistant_model = meta.get("assistant_model") or self._resolve_chat_model()

            log.infox(
                "voice_live:updater_loop:start",
                thread_id=thread_id,
                run_dir=str(run_dir),
                model=assistant_model,
                update_every_s=self.update_every_s,
            )

            with log_context(thread_id=thread_id, run_dir=str(run_dir), model=assistant_model):
                self._write_status(
                    run_dir,
                    state=self.STATE_RUNNING,
                    step="updater_started",
                    progress=0.01,
                    message="Updater running.",
                    error=None,
                )

                while True:
                    st = read_json(run_dir / "status.json") or {}

                    log.debugx(
                        "voice_live:updater_loop:tick",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        state=st.get("state"),
                        step=st.get("step"),
                        queue_marker_exists=(run_dir / ".new_transcript").exists(),
                    )

                    if st.get("state") in {self.STATE_DONE, self.STATE_FAILED}:
                        log.infox(
                            "voice_live:updater_loop:stop_state_seen",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            state=st.get("state"),
                        )
                        return

                    if (run_dir / ".new_transcript").exists():
                        lock = self._get_lock(str(run_dir))
                        async with lock:
                            log.debugx(
                                "voice_live:updater_loop:new_transcript_lock_acquired",
                                thread_id=thread_id,
                                run_dir=str(run_dir),
                            )

                            try:
                                (run_dir / ".new_transcript").unlink(missing_ok=True)
                                log.debugx(
                                    "voice_live:updater_loop:marker_removed",
                                    thread_id=thread_id,
                                    run_dir=str(run_dir),
                                )
                            except Exception:
                                log.warningx(
                                    "voice_live:updater_loop:marker_remove_failed",
                                    thread_id=thread_id,
                                    run_dir=str(run_dir),
                                )

                            try:
                                log.infox(
                                    "voice_live:updater_loop:update_state_start",
                                    thread_id=thread_id,
                                    run_dir=str(run_dir),
                                )
                                await self._update_state_from_new_transcript(run_dir)
                                log.infox(
                                    "voice_live:updater_loop:update_state_done",
                                    thread_id=thread_id,
                                    run_dir=str(run_dir),
                                )
                            except Exception as e:
                                err = {"error": str(e), "type": type(e).__name__, "utc": utc_iso()}
                                write_json(run_dir / "error.json", err)
                                self._write_status(
                                    run_dir,
                                    state=self.STATE_FAILED,
                                    step="update_failed",
                                    progress=1.0,
                                    message="Live update failed.",
                                    error=err,
                                )
                                log.exception("voice_live:update_failed")
                                return

                    await asyncio.sleep(self.update_every_s)

        self._run_update_tasks[key] = asyncio.create_task(_loop())

        log.infox(
            "voice_live:ensure_updater:created",
            run_dir=str(run_dir),
            key=key,
            task_count=len(self._run_update_tasks),
        )

    async def _update_state_from_new_transcript(self, run_dir: Path) -> None:
        log.infox(
            "voice_live:update_state:start",
            run_dir=str(run_dir),
        )

        meta = read_json(run_dir / "meta.json") or {}
        thread_id = meta.get("thread_id") or "unknown"
        assistant_model = meta.get("assistant_model") or self._resolve_chat_model()
        payload = meta.get("payload") or {}

        log.debugx(
            "voice_live:update_state:meta_loaded",
            thread_id=thread_id,
            run_dir=str(run_dir),
            assistant_model=assistant_model,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
        )

        lines = read_jsonl(run_dir / "live_transcript.jsonl")
        if not lines:
            log.debugx(
                "voice_live:update_state:no_transcript_lines",
                thread_id=thread_id,
                run_dir=str(run_dir),
            )
            return

        progress_path = run_dir / "live_progress.json"
        prog = read_json(progress_path) or {}
        last_chunk = int(prog.get("last_chunk_index") or -1)

        new_lines = [ln for ln in lines if int(ln.get("chunk_index") or -1) > last_chunk]
        if not new_lines:
            log.debugx(
                "voice_live:update_state:no_new_lines",
                thread_id=thread_id,
                run_dir=str(run_dir),
                total_lines=len(lines),
                last_chunk=last_chunk,
            )
            return

        delta_text = "\n".join([(ln.get("text") or "").strip() for ln in new_lines]).strip()
        if not delta_text:
            write_json(
                progress_path,
                {"last_chunk_index": int(new_lines[-1]["chunk_index"]), "updated_utc": utc_iso()},
            )
            log.debugx(
                "voice_live:update_state:empty_delta_progress_updated",
                thread_id=thread_id,
                run_dir=str(run_dir),
                last_chunk_index=int(new_lines[-1]["chunk_index"]),
            )
            return

        delta_start = new_lines[0].get("start_s")
        delta_end = new_lines[-1].get("end_s")

        log.infox(
            "voice_live:update_state:new_delta",
            thread_id=thread_id,
            run_dir=str(run_dir),
            total_lines=len(lines),
            new_line_count=len(new_lines),
            last_chunk=last_chunk,
            first_new_chunk=new_lines[0].get("chunk_index"),
            last_new_chunk=new_lines[-1].get("chunk_index"),
            delta_text_len=len(delta_text),
            delta_start=delta_start,
            delta_end=delta_end,
        )

        profile = get_profile((payload or {}).get("profile"))

        log.debugx(
            "voice_live:update_state:profile_selected",
            thread_id=thread_id,
            run_dir=str(run_dir),
            profile_id=getattr(profile, "id", None),
        )

        state = read_json(run_dir / "state.json")
        if not isinstance(state, dict):
            log.warningx(
                "voice_live:update_state:state_invalid_using_empty",
                thread_id=thread_id,
                run_dir=str(run_dir),
                state_type=type(state).__name__,
            )
            state = profile.empty_state()

        self._write_status(
            run_dir,
            state=self.STATE_UPDATING,
            step=f"llm_update_{int(new_lines[0]['chunk_index']):06d}_{int(new_lines[-1]['chunk_index']):06d}",
            progress=self._rough_progress(run_dir),
            message=f"Updating notes {fmt_timerange(delta_start, delta_end)}.",
            error=None,
        )

        profile = get_profile((payload or {}).get("profile"))
        assistant = profile.assistant()

        log.debugx(
            "voice_live:update_state:assistant_selected",
            thread_id=thread_id,
            run_dir=str(run_dir),
            profile_id=getattr(profile, "id", None),
            assistant_type=type(assistant).__name__,
        )

        prompt = assistant.prompt(
            "",
            current_state=state,
            delta_transcript=delta_text,
            delta_time_range={"start_s": delta_start, "end_s": delta_end},
            **payload,
        )
        write_text(run_dir / "assistant_prompt_last.txt", prompt)

        log.infox(
            "voice_live:update_state:llm_call_start",
            thread_id=thread_id,
            run_dir=str(run_dir),
            model=assistant_model,
            prompt_len=len(prompt or ""),
            delta_text_len=len(delta_text),
            keep_context=self.keep_context,
        )

        rr: ResponseResult = await asyncio.to_thread(
            self.responses.ask,
            [{"role": "user", "content": prompt}],
            session_id=thread_id,
            keep_context=self.keep_context,
            model=assistant_model,
            instructions=assistant.instructions,
        )
        write_text(run_dir / "assistant_raw_last.txt", rr.text)

        log.infox(
            "voice_live:update_state:llm_call_done",
            thread_id=thread_id,
            run_dir=str(run_dir),
            response_text_len=len(rr.text or ""),
        )

        data_any = assistant.extract_first_json_object(rr.text)
        if not isinstance(data_any, dict):
            log.warningx(
                "voice_live:update_state:invalid_json",
                thread_id=thread_id,
                run_dir=str(run_dir),
                extracted_type=type(data_any).__name__,
                response_text_len=len(rr.text or ""),
            )
            raise ValueError(f"VoiceLiveAssistant returned non-object JSON: {type(data_any)}")

        log.debugx(
            "voice_live:update_state:json_extracted",
            thread_id=thread_id,
            run_dir=str(run_dir),
            data_keys=list(data_any.keys()),
        )

        write_json(run_dir / "state.json", data_any)
        rendered = profile.render(data_any)
        write_text(run_dir / "markdown.md", rendered)

        write_json(progress_path, {"last_chunk_index": int(new_lines[-1]["chunk_index"]), "updated_utc": utc_iso()})

        # Meeting-driven actions (#9) — fan a read-only action pass off this
        # delta on a DETACHED task so a slow look-up never blocks the note lane
        # or the FE draft polling. No-op unless the profile opts in (action_policy).
        try:
            from services.voice.meeting_action_service import process_delta as _meeting_process_delta
            _action_task = asyncio.create_task(
                _meeting_process_delta(
                    run_dir=run_dir,
                    thread_id=thread_id,
                    profile_id=(payload or {}).get("profile"),
                    delta_text=delta_text,
                    state=data_any,
                )
            )
            self._action_tasks.add(_action_task)
            _action_task.add_done_callback(self._action_tasks.discard)
        except Exception as _e:  # noqa: BLE001 — actions must never affect note-taking
            log.warningx("voice_live:meeting_action:spawn_failed", run_dir=str(run_dir), error=str(_e))

        log.infox(
            "voice_live:update_state:artifacts_written",
            thread_id=thread_id,
            run_dir=str(run_dir),
            last_chunk_index=int(new_lines[-1]["chunk_index"]),
            markdown_len=len(rendered or ""),
        )

        self._write_status(
            run_dir,
            state=self.STATE_RUNNING,
            step="idle",
            progress=self._rough_progress(run_dir),
            message="Waiting for more audio…",
            error=None,
        )

        log.infox(
            "voice_live:update_state:done",
            thread_id=thread_id,
            run_dir=str(run_dir),
            last_chunk_index=int(new_lines[-1]["chunk_index"]),
        )

    # ---------------------------------------------------------------------
    # Transcription: real tail extraction (shared utilities)
    # ---------------------------------------------------------------------
    async def _transcribe_live_window_async(self, run_dir: Path) -> Tuple[str, str, float]:
        log.infox(
            "voice_live:transcribe_window:start",
            run_dir=str(run_dir),
            transcription_model=self.transcription_model,
            transcribe_tail_s=self.transcribe_tail_s,
            probe_first_s=self.probe_first_s,
            min_wav_s=self.min_wav_s,
        )

        recording_path = run_dir / "recording.webm"
        tail_s = float(self.transcribe_tail_s)
        probe_s = float(self.probe_first_s)
        min_s = float(self.min_wav_s)

        def _call() -> Tuple[str, str, float]:
            log.debugx(
                "voice_live:transcribe_window:sync_call_start",
                run_dir=str(run_dir),
                recording_path=str(recording_path),
                recording_exists=recording_path.exists(),
                recording_bytes=recording_path.stat().st_size if recording_path.exists() else 0,
            )

            dur = ffprobe_duration_s(recording_path)
            start = None
            dur_s = None
            method = "probe_first"

            if dur is not None and dur > 0.1:
                start = max(0.0, dur - tail_s)
                dur_s = min(tail_s, dur)
                method = "duration_tail"

            log.debugx(
                "voice_live:transcribe_window:segment_plan",
                run_dir=str(run_dir),
                ffprobe_duration_s=dur,
                start_s=start,
                dur_s=dur_s,
                method=method,
            )

            wav, rc, stderr = webm_file_segment_to_wav_bytes(recording_path, start_s=start, dur_s=dur_s)
            wav_dur = wav_duration_s(wav) if wav else 0.0

            log.debugx(
                "voice_live:transcribe_window:first_segment",
                run_dir=str(run_dir),
                method=method,
                ffmpeg_rc=rc,
                wav_bytes=len(wav or b""),
                wav_dur_s=wav_dur,
                stderr_preview=(stderr or "")[:300],
            )

            if wav_dur < min_s:
                wav, rc, stderr = webm_file_segment_to_wav_bytes(recording_path, start_s=0.0, dur_s=probe_s)
                wav_dur = wav_duration_s(wav) if wav else 0.0
                method = "probe_first"

                log.debugx(
                    "voice_live:transcribe_window:probe_fallback",
                    run_dir=str(run_dir),
                    ffmpeg_rc=rc,
                    wav_bytes=len(wav or b""),
                    wav_dur_s=wav_dur,
                    stderr_preview=(stderr or "")[:300],
                )

            if wav_dur < min_s:
                log.infox("voice_live:wav_too_short", dur_s=wav_dur)
                return "", method, wav_dur

            use_model = self._resolve_transcription_model(self.transcription_model)
            log.infox(
                "voice_live:transcribe_window:openai_transcription_start",
                run_dir=str(run_dir),
                method=method,
                wav_dur_s=wav_dur,
                wav_bytes=len(wav or b""),
                model=use_model,
            )

            resp = self.client.audio.translations.create(
                model=use_model,
                file=("window.wav", wav, "audio/wav"),
            )
            text = (getattr(resp, "text", "") or "").strip()

            log.infox(
                "voice_live:transcribe_window:openai_transcription_done",
                run_dir=str(run_dir),
                method=method,
                wav_dur_s=wav_dur,
                text_len=len(text),
            )
            return text, method, wav_dur

        result = await asyncio.to_thread(_call)

        log.infox(
            "voice_live:transcribe_window:done",
            run_dir=str(run_dir),
            text_len=len(result[0] or ""),
            method=result[1],
            wav_dur_s=result[2],
        )
        return result

    async def _final_cleanup_markdown(self, run_dir: Path) -> None:
        log.infox(
            "voice_live:final_cleanup:start",
            run_dir=str(run_dir),
        )

        meta = read_json(run_dir / "meta.json") or {}
        thread_id = meta.get("thread_id") or "unknown"
        assistant_model = meta.get("assistant_model") or self._resolve_chat_model()
        payload = meta.get("payload") or {}

        log.debugx(
            "voice_live:final_cleanup:meta_loaded",
            thread_id=thread_id,
            run_dir=str(run_dir),
            assistant_model=assistant_model,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
        )

        state = read_json(run_dir / "state.json")
        if not isinstance(state, dict):
            log.warningx(
                "voice_live:final_cleanup:state_invalid_skip",
                thread_id=thread_id,
                run_dir=str(run_dir),
                state_type=type(state).__name__,
            )
            return

        profile = get_profile((payload or {}).get("profile"))
        finalizer = profile.finalizer()
        if not finalizer:
            log.infox(
                "voice_live:final_cleanup:no_finalizer_skip",
                thread_id=thread_id,
                run_dir=str(run_dir),
                profile_id=getattr(profile, "id", None),
            )
            return

        log.debugx(
            "voice_live:final_cleanup:finalizer_selected",
            thread_id=thread_id,
            run_dir=str(run_dir),
            profile_id=getattr(profile, "id", None),
            finalizer_type=type(finalizer).__name__,
        )

        prompt = finalizer.prompt("", state=state, **payload)
        write_text(run_dir / "finalize_prompt_last.txt", prompt)

        log.infox(
            "voice_live:final_cleanup:llm_call_start",
            thread_id=thread_id,
            run_dir=str(run_dir),
            model=assistant_model,
            prompt_len=len(prompt or ""),
            keep_context=self.keep_context,
        )

        rr: ResponseResult = await asyncio.to_thread(
            self.responses.ask,
            [{"role": "user", "content": prompt}],
            session_id=thread_id,
            keep_context=self.keep_context,
            model=assistant_model,
            instructions=finalizer.instructions,
        )

        log.infox(
            "voice_live:final_cleanup:llm_call_done",
            thread_id=thread_id,
            run_dir=str(run_dir),
            response_text_len=len(rr.text or ""),
        )

        response = finalizer.extract_first_json_object(rr.text)

        log.debugx(
            "voice_live:final_cleanup:response_extracted",
            thread_id=thread_id,
            run_dir=str(run_dir),
            response_type=type(response).__name__,
            response_keys=list(response.keys()) if isinstance(response, dict) else None,
        )

        title = (response.get("title", f"{thread_id}_Meeting_Notes") or "").strip()
        md = (response.get("text") or "").strip()
        if md:
            log.infox(
                "voice_live:final_cleanup:markdown_ready",
                thread_id=thread_id,
                run_dir=str(run_dir),
                title=title,
                markdown_len=len(md),
            )

            write_text(run_dir / "markdown.md", md)
            write_text(run_dir / "finalize_raw_last.txt", response.get("text") or "")

            log.infox(
                "voice_live:final_cleanup:text_ingest_start",
                thread_id=thread_id,
                run_dir=str(run_dir),
                title=title,
            )

            from services.assistants.ask_job_callbacks import text_indexer
            from services.text.text_storage_service import IncomingText
            await asyncio.to_thread(
                text_indexer.ingest_text,
                IncomingText(
                    source="ui",
                    title=title,
                    content=md,
                    subdir="Meeting Notes",
                ),
            )
            log.infox(
                "voice_live:final_cleanup:text_ingest_done",
                thread_id=thread_id,
                run_dir=str(run_dir),
                title=title,
            )
        else:
            log.infox(
                "voice_live:final_cleanup:no_markdown_skip_ingest",
                thread_id=thread_id,
                run_dir=str(run_dir),
                title=title,
            )

    # ---------------------------------------------------------------------
    # Run dir / locking / IO
    # ---------------------------------------------------------------------
    def _make_run_dir(self, *, thread_id: str, kind: str, original_filename: str) -> Path:
        log.debugx(
            "voice_live:make_run_dir:start",
            thread_id=thread_id,
            kind=kind,
            original_filename=original_filename,
            voice_root=str(self.voice_root),
        )

        tid = safe_slug(thread_id)
        stem = safe_slug(Path(original_filename).stem, max_len=40, fallback="file")
        run_id = f"voice_{kind}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}_{stem}_{uuid.uuid4().hex[:8]}"
        run_dir = self.voice_root / tid / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        log.debugx(
            "voice_live:make_run_dir:done",
            thread_id=thread_id,
            safe_thread_id=tid,
            stem=stem,
            run_id=run_id,
            run_dir=str(run_dir),
            exists=run_dir.exists(),
        )
        return run_dir

    def _resolve_run_dir(self, *, thread_id: str, run_id: str) -> Path:
        log.debugx(
            "voice_live:resolve_run_dir:start",
            thread_id=thread_id,
            run_id=run_id,
            voice_root=str(self.voice_root),
        )

        run_dir = self.voice_root / safe_slug(thread_id) / run_id
        if not run_dir.exists():
            log.warningx(
                "voice_live:resolve_run_dir:not_found",
                thread_id=thread_id,
                run_id=run_id,
                run_dir=str(run_dir),
            )
            raise FileNotFoundError(f"Run dir not found: {run_dir}")

        log.debugx(
            "voice_live:resolve_run_dir:done",
            thread_id=thread_id,
            run_id=run_id,
            run_dir=str(run_dir),
        )
        return run_dir

    def _get_lock(self, key: str) -> asyncio.Lock:
        log.debugx(
            "voice_live:get_lock:start",
            key=key,
            exists=key in self._run_locks,
            lock_count=len(self._run_locks),
        )

        lock = self._run_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[key] = lock
            log.debugx(
                "voice_live:get_lock:created",
                key=key,
                lock_count=len(self._run_locks),
            )
        else:
            log.debugx(
                "voice_live:get_lock:existing",
                key=key,
                lock_count=len(self._run_locks),
            )
        return lock

    def _artifact_paths(self, run_dir: Path) -> Dict[str, str]:
        log.debugx(
            "voice_live:artifact_paths:start",
            run_dir=str(run_dir),
        )

        files = [
            "meta.json",
            "status.json",
            "state.json",
            "markdown.md",
            "live_transcript.jsonl",
            "assistant_prompt_last.txt",
            "assistant_raw_last.txt",
            "live_progress.json",
            "error.json",
            "actions.jsonl",
            "recording.webm",
            "finalize_prompt_last.txt",
            "finalize_raw_last.txt",
        ]
        out: Dict[str, str] = {}
        for f in files:
            p = run_dir / f
            if p.exists():
                out[f] = str(p)
        chunks = run_dir / "chunks"
        if chunks.exists():
            out["chunks_dir"] = str(chunks)

        log.debugx(
            "voice_live:artifact_paths:done",
            run_dir=str(run_dir),
            artifact_count=len(out),
            artifact_keys=list(out.keys()),
        )
        return out

    def _write_status(
        self,
        run_dir: Path,
        *,
        state: str,
        step: str,
        progress: float,
        message: str,
        error: Optional[Dict[str, Any]],
    ) -> None:
        log.debugx(
            "voice_live:write_status:start",
            run_dir=str(run_dir),
            state=state,
            step=step,
            progress=progress,
            message=message,
            has_error=error is not None,
        )

        status_path = run_dir / "status.json"
        existing = read_json(status_path) or {}
        created_utc = existing.get("created_utc") or utc_iso()

        status = {
            "state": state,
            "step": step,
            "progress": float(max(0.0, min(1.0, progress))),
            "message": message,
            "created_utc": created_utc,
            "updated_utc": utc_iso(),
            "error": error,
            "artifacts": self._artifact_paths(run_dir),
        }
        atomic_write_json(status_path, status)

        log.infox(
            "voice_live:write_status:done",
            run_dir=str(run_dir),
            status_path=str(status_path),
            state=state,
            step=step,
            progress=status["progress"],
            artifact_count=len(status.get("artifacts") or {}),
            has_error=error is not None,
        )

    def _rough_progress(self, run_dir: Path) -> float:
        chunks_dir = run_dir / "chunks"
        n = len(list(chunks_dir.glob("chunk_*"))) if chunks_dir.exists() else 0
        result = float(min(0.95, 0.05 + n * 0.01))

        log.debugx(
            "voice_live:rough_progress",
            run_dir=str(run_dir),
            chunks_dir=str(chunks_dir),
            chunks_dir_exists=chunks_dir.exists(),
            chunk_count=n,
            progress=result,
        )
        return result

    # ---------------------------------------------------------------------
    # Defaults
    # ---------------------------------------------------------------------
    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        log.debugx("voice_live:empty_state:create")
        return {
            "views": {"exec": "", "detailed": "", "bullets": []},
            "highlights": [],
            "action_items": [],
            "decision_log": [],
            "sentiment": {"overall": "unclear", "signals": [], "confidence": "low"},
            "supportive_questions": [],
            "open_questions": [],
            "notes": [],
        }