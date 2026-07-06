# services/voice_service.py
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from assistants.voice.voice_assistant import VoiceAssistant
from component.logging import get_logger, StepSequence, log_context

from services.openai_service import OpenAIResponsesService, ResponseResult

from services.voice.voice_utilities import (
    safe_slug,
    utc_iso,
    utc_stamp,
    write_json,
    read_json,
    write_text,
    atomic_write_json,
)
from services.markdown.renderer import default_markdown_service

log = get_logger("svc.voice")


# -----------------------------
# Data models
# -----------------------------
@dataclass
class WordTS:
    start_s: float
    end_s: float
    word: str


@dataclass
class DiarizedSegment:
    start_s: float
    end_s: float
    speaker: str  # e.g. "S1"
    text: str


@dataclass
class SpeakerWord:
    start_s: float
    end_s: float
    speaker: str
    word: str


@dataclass
class RichTranscript:
    text: str
    diarized_segments: List[DiarizedSegment]
    words: List[SpeakerWord]
    speakers: List[Dict[str, Any]]


@dataclass
class VoiceServiceResult:
    transcript: str
    data: Dict[str, Any]
    markdown: str
    response_id: str
    raw: Any
    rich: Optional[Dict[str, Any]] = None
    run_dir: Optional[str] = None


class VoiceService:
    """
    Chunk splitting (ffmpeg) + polling-ready job files + MCP ingest of FINAL markdown.

    Refactor:
      - uses shared utilities (voice_utilities) for IO + slug + timestamps
      - uses shared markdown renderer (services.markdown.renderer)
      - removed local json_to_markdown (renderer is source of truth)
    """

    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_TRANSCRIBING = "transcribing"
    STATE_CHUNKING = "chunking"
    STATE_ALIGNING = "aligning"
    STATE_EXTRACTING = "extracting"
    STATE_RENDERING = "rendering"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(
        self,
        responses: OpenAIResponsesService,
        *,
        client: Optional[OpenAI] = None,
        # None => resolve from the 'transcription' slot at call time (no hardcoded
        # default). Explicit values still override the slot when provided.
        diarize_model: Optional[str] = None,
        wordts_model: Optional[str] = None,
        keep_context: bool = True,
        voice_root: str | Path = "voice",
        max_bytes_in_memory: int = 100 * 1024 * 1024,
        enable_chunking: bool = True,
        chunk_seconds: int = 120,
        min_duration_for_chunking_s: int = 240,
        force_chunk_wav: bool = True,
        max_segments_per_assistant_call: int = 180,
        assistant_parallelism: int = 2,
        enable_mcp_ingest: bool = True,
        mcp_poll_until_done: bool = False,
        mcp_poll_interval_s: float = 1.5,
        mcp_poll_timeout_s: float = 45.0,
        mcp_subdir_prefix: str = "voice",
    ):
        log.infox(
            "voice:init:start",
            has_responses=responses is not None,
            client_provided=client is not None,
            diarize_model=diarize_model,
            wordts_model=wordts_model,
            keep_context=keep_context,
            voice_root=str(voice_root),
            max_bytes_in_memory=max_bytes_in_memory,
            enable_chunking=enable_chunking,
            chunk_seconds=chunk_seconds,
            min_duration_for_chunking_s=min_duration_for_chunking_s,
            force_chunk_wav=force_chunk_wav,
            max_segments_per_assistant_call=max_segments_per_assistant_call,
            assistant_parallelism=assistant_parallelism,
            enable_mcp_ingest=enable_mcp_ingest,
            mcp_poll_until_done=mcp_poll_until_done,
            mcp_poll_interval_s=mcp_poll_interval_s,
            mcp_poll_timeout_s=mcp_poll_timeout_s,
            mcp_subdir_prefix=mcp_subdir_prefix,
        )

        self.responses = responses
        # Resolve the OpenAI SDK client lazily (see the `client` property): voice
        # is the only consumer, so a missing OpenAI key must never block startup.
        self._client = client
        self.diarize_model = diarize_model
        self.wordts_model = wordts_model
        self.keep_context = bool(keep_context)

        self.voice_root = Path(voice_root)
        self.voice_root.mkdir(parents=True, exist_ok=True)
        self.max_bytes_in_memory = int(max_bytes_in_memory)

        self.enable_chunking = bool(enable_chunking)
        self.chunk_seconds = int(max(10, chunk_seconds))
        self.min_duration_for_chunking_s = int(max(0, min_duration_for_chunking_s))
        self.force_chunk_wav = bool(force_chunk_wav)

        self.max_segments_per_assistant_call = int(max(30, max_segments_per_assistant_call))
        self.assistant_parallelism = int(max(1, assistant_parallelism))

        self._ffmpeg = shutil.which("ffmpeg")
        self._ffprobe = shutil.which("ffprobe")

        self.enable_mcp_ingest = bool(enable_mcp_ingest)
        self.mcp_poll_until_done = bool(mcp_poll_until_done)
        self.mcp_poll_interval_s = float(max(0.25, mcp_poll_interval_s))
        self.mcp_poll_timeout_s = float(max(1.0, mcp_poll_timeout_s))
        self.mcp_subdir_prefix = (mcp_subdir_prefix or "voice").strip().strip("/")

        log.debugx(
            "voice:init:dependencies_detected",
            ffmpeg=self._ffmpeg,
            ffprobe=self._ffprobe,
            has_client=self._client is not None,
            voice_root=str(self.voice_root),
            voice_root_exists=self.voice_root.exists(),
        )

    @property
    def client(self):
        """OpenAI SDK client, built on first use. Deferred so the app boots
        without an OpenAI key; only the voice feature actually needs it."""
        if self._client is None:
            self._client = self.responses.client
        return self._client

        log.infox(
            "voice:init:done",
            voice_root=str(self.voice_root),
            diarize_model=self.diarize_model,
            wordts_model=self.wordts_model,
            keep_context=self.keep_context,
            max_bytes_in_memory=self.max_bytes_in_memory,
            enable_chunking=self.enable_chunking,
            chunk_seconds=self.chunk_seconds,
            min_duration_for_chunking_s=self.min_duration_for_chunking_s,
            force_chunk_wav=self.force_chunk_wav,
            max_segments_per_assistant_call=self.max_segments_per_assistant_call,
            assistant_parallelism=self.assistant_parallelism,
            enable_mcp_ingest=self.enable_mcp_ingest,
            mcp_poll_until_done=self.mcp_poll_until_done,
            mcp_poll_interval_s=self.mcp_poll_interval_s,
            mcp_poll_timeout_s=self.mcp_poll_timeout_s,
            mcp_subdir_prefix=self.mcp_subdir_prefix,
        )

    # =============================================================
    # Job / polling-ready API
    # =============================================================
    async def start_voice_job(
        self,
        audio_file: Any,
        *,
        thread_id: str,
        model: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        log.infox(
            "voice:start_job:start",
            thread_id=thread_id,
            model=model,
            payload_keys=list((payload or {}).keys()),
            audio_type=type(audio_file).__name__,
        )

        payload = payload or {}

        audio_bytes, filename, content_type = await self._read_audio_bytes(audio_file)

        log.debugx(
            "voice:start_job:audio_read",
            thread_id=thread_id,
            model=model,
            filename=filename,
            content_type=content_type,
            bytes_count=len(audio_bytes or b""),
        )

        if not audio_bytes:
            log.warningx(
                "voice:start_job:empty_audio",
                thread_id=thread_id,
                filename=filename,
                content_type=content_type,
            )
            raise ValueError("Empty audio upload (0 bytes). Did the client send audio correctly?")

        if len(audio_bytes) > self.max_bytes_in_memory:
            log.warningx(
                "voice:start_job:audio_too_large",
                thread_id=thread_id,
                filename=filename,
                bytes_count=len(audio_bytes),
                max_bytes_in_memory=self.max_bytes_in_memory,
            )
            raise ValueError(
                f"Audio too large for in-memory staging ({len(audio_bytes)} bytes). "
                "Increase max_bytes_in_memory or implement streaming/chunk upload."
            )

        run_dir = self._make_run_dir(thread_id=thread_id, original_filename=filename)
        audio_path = self._write_audio(run_dir, audio_bytes, filename=filename, content_type=content_type)

        log.debugx(
            "voice:start_job:audio_written",
            thread_id=thread_id,
            run_id=run_dir.name,
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            bytes_count=len(audio_bytes),
        )

        meta = {
            "thread_id": thread_id,
            "run_id": run_dir.name,
            "created_utc": utc_iso(),
            "original_filename": filename,
            "content_type": content_type,
            "bytes": len(audio_bytes),
            "diarize_model": self.diarize_model,
            "wordts_model": self.wordts_model,
            "assistant_model": model,
            "audio_path": str(audio_path),
            "payload": payload,
            "chunking": {
                "enabled": self.enable_chunking,
                "chunk_seconds": self.chunk_seconds,
                "min_duration_for_chunking_s": self.min_duration_for_chunking_s,
                "force_chunk_wav": self.force_chunk_wav,
            },
            "mcp": {
                "enabled": self.enable_mcp_ingest,
                "poll_until_done": self.mcp_poll_until_done,
                "subdir_prefix": self.mcp_subdir_prefix,
            },
        }

        log.debugx(
            "voice:start_job:write_meta",
            thread_id=thread_id,
            run_id=run_dir.name,
            meta_keys=list(meta.keys()),
        )
        write_json(run_dir / "meta.json", meta)

        self._write_status(
            run_dir,
            state=self.STATE_QUEUED,
            step="created",
            progress=0.0,
            message="Job created. Awaiting processing.",
            error=None,
        )

        result = {"thread_id": thread_id, "run_id": run_dir.name, "run_dir": str(run_dir), "audio_path": str(audio_path)}

        log.infox(
            "voice:start_job:done",
            thread_id=thread_id,
            run_id=run_dir.name,
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            bytes_count=len(audio_bytes),
        )
        return result

    async def process_voice_job(
        self,
        *,
        run_dir: str | Path,
        model: Optional[str] = None,
        timeout_s: float = 60 * 60,
    ) -> VoiceServiceResult:
        log.infox(
            "voice:process_job:start",
            run_dir=str(run_dir),
            model=model,
            timeout_s=timeout_s,
        )

        run_dir = Path(run_dir)
        if not run_dir.exists():
            log.warningx(
                "voice:process_job:run_dir_missing",
                run_dir=str(run_dir),
            )
            raise FileNotFoundError(f"Run dir not found: {run_dir}")

        meta = read_json(run_dir / "meta.json") or {}
        thread_id = meta.get("thread_id") or "unknown"
        # Chat model is slot-driven (no hardcoded fallback): explicit request >
        # stored job model > first assigned chat slot.
        assistant_model = model or meta.get("assistant_model") or self._resolve_chat_model()
        payload = meta.get("payload") or {}

        log.debugx(
            "voice:process_job:meta_loaded",
            thread_id=thread_id,
            run_id=run_dir.name,
            run_dir=str(run_dir),
            assistant_model=assistant_model,
            meta_keys=list(meta.keys()) if isinstance(meta, dict) else None,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
        )

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        deadline = t0 + float(timeout_s)

        def remaining_s() -> float:
            remaining = max(0.0, deadline - loop.time())
            log.debugx(
                "voice:process_job:remaining_time",
                thread_id=thread_id,
                run_dir=str(run_dir),
                remaining_s=remaining,
            )
            return remaining

        with log_context(
            thread_id=thread_id,
            run_dir=str(run_dir),
            model=assistant_model,
            diarize_model=self.diarize_model,
            wordts_model=self.wordts_model,
        ):
            lock = self._acquire_lock(run_dir)
            try:
                log.debugx(
                    "voice:process_job:lock_acquired",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    lock_path=str(lock) if lock else None,
                )

                self._write_status(run_dir, state=self.STATE_RUNNING, step="start", progress=0.01, message="Starting.", error=None)

                audio_path = Path(meta.get("audio_path") or "")
                if not audio_path.exists():
                    log.warningx(
                        "voice:process_job:meta_audio_missing_lookup_fallback",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        meta_audio_path=str(audio_path),
                    )
                    audio_files = sorted(run_dir.glob("audio.*"))
                    if not audio_files:
                        log.warningx(
                            "voice:process_job:audio_not_found",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                        )
                        raise FileNotFoundError("Audio not found in run dir.")
                    audio_path = audio_files[0]

                log.infox(
                    "voice:process_job:audio_selected",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    audio_path=str(audio_path),
                    audio_exists=audio_path.exists(),
                    audio_bytes=audio_path.stat().st_size if audio_path.exists() else 0,
                )

                content_type = meta.get("content_type") or "application/octet-stream"

                if self._ffmpeg:
                    diarize_path = run_dir / "diarized.wav"
                    log.infox(
                        "voice:process_job:convert_diarize_wav_start",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        src=str(audio_path),
                        dst=str(diarize_path),
                    )
                    self._ffmpeg_convert_to_wav(audio_path, diarize_path)
                    log.infox(
                        "voice:process_job:convert_diarize_wav_done",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        dst=str(diarize_path),
                        bytes_count=diarize_path.stat().st_size if diarize_path.exists() else 0,
                    )
                else:
                    diarize_path = audio_path
                    log.warningx(
                        "voice:process_job:ffmpeg_missing_using_original_for_diarize",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        audio_path=str(audio_path),
                    )

                duration_s = self._ffprobe_duration_s(audio_path) if (self.enable_chunking and self._ffprobe) else None
                if duration_s is not None:
                    write_json(run_dir / "duration.json", {"duration_s": duration_s, "updated_utc": utc_iso()})
                    log.infox(
                        "voice:process_job:duration_detected",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        audio_path=str(audio_path),
                        duration_s=duration_s,
                    )
                else:
                    log.debugx(
                        "voice:process_job:duration_not_available",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        enable_chunking=self.enable_chunking,
                        has_ffprobe=bool(self._ffprobe),
                    )

                self._write_status(
                    run_dir,
                    state=self.STATE_TRANSCRIBING,
                    step="diarized_full",
                    progress=0.08,
                    message="Transcribing diarization (full recording).",
                    error=None,
                )

                log.infox(
                    "voice:process_job:diarized_transcription_start",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    diarize_path=str(diarize_path),
                    content_type=content_type,
                    diarize_model=self.diarize_model,
                )
                diarized = await asyncio.wait_for(
                    self._transcribe_diarized_async(diarize_path, content_type=content_type),
                    timeout=min(remaining_s(), 3600.0),
                )
                write_json(run_dir / "diarized.json", diarized)
                log.infox(
                    "voice:process_job:diarized_transcription_done",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    diarized_keys=list(diarized.keys()) if isinstance(diarized, dict) else None,
                    segment_count=len((diarized or {}).get("segments") or (diarized or {}).get("speaker_segments") or (diarized or {}).get("utterances") or []) if isinstance(diarized, dict) else None,
                )

                chunks_index: List[Dict[str, Any]] = []
                do_chunk = bool(
                    self.enable_chunking
                    and self._ffmpeg
                    and self._ffprobe
                    and duration_s is not None
                    and duration_s >= self.min_duration_for_chunking_s
                )

                log.infox(
                    "voice:process_job:chunking_decision",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    do_chunk=do_chunk,
                    enable_chunking=self.enable_chunking,
                    has_ffmpeg=bool(self._ffmpeg),
                    has_ffprobe=bool(self._ffprobe),
                    duration_s=duration_s,
                    min_duration_for_chunking_s=self.min_duration_for_chunking_s,
                )

                if do_chunk:
                    self._write_status(
                        run_dir,
                        state=self.STATE_CHUNKING,
                        step="ffmpeg_split",
                        progress=0.20,
                        message=f"Splitting audio into ~{self.chunk_seconds}s chunks.",
                        error=None,
                    )
                    chunks_index = self._split_audio_into_chunks(run_dir, audio_path, duration_s=duration_s)
                    log.infox(
                        "voice:process_job:chunking_done",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        chunk_count=len(chunks_index),
                    )
                else:
                    chunks_dir = run_dir / "chunks"
                    chunks_dir.mkdir(parents=True, exist_ok=True)
                    if self.force_chunk_wav and self._ffmpeg:
                        chunk_path = chunks_dir / "chunk_00000.wav"
                        log.infox(
                            "voice:process_job:single_chunk_wav_convert_start",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            src=str(audio_path),
                            dst=str(chunk_path),
                        )
                        self._ffmpeg_convert_to_wav(audio_path, chunk_path)
                        log.infox(
                            "voice:process_job:single_chunk_wav_convert_done",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            chunk_path=str(chunk_path),
                            bytes_count=chunk_path.stat().st_size if chunk_path.exists() else 0,
                        )
                    else:
                        chunk_path = chunks_dir / f"chunk_00000{audio_path.suffix or '.bin'}"
                        if not chunk_path.exists():
                            shutil.copy2(audio_path, chunk_path)
                            log.debugx(
                                "voice:process_job:single_chunk_copied",
                                thread_id=thread_id,
                                run_dir=str(run_dir),
                                src=str(audio_path),
                                dst=str(chunk_path),
                            )

                    chunks_index = [{
                        "index": 0,
                        "offset_s": 0.0,
                        "path": str(chunk_path),
                        "duration_s": float(duration_s) if duration_s is not None else None,
                    }]
                    write_json(chunks_dir / "chunks.json", {"chunks": chunks_index, "updated_utc": utc_iso()})
                    log.infox(
                        "voice:process_job:single_chunk_ready",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        chunk_path=str(chunk_path),
                        duration_s=duration_s,
                    )

                self._write_status(
                    run_dir,
                    state=self.STATE_TRANSCRIBING,
                    step="wordts_chunks",
                    progress=0.30,
                    message=f"Transcribing word timestamps ({len(chunks_index)} chunk(s)).",
                    error=None,
                )

                merged_words: List[Dict[str, Any]] = []
                chunks_dir = run_dir / "chunks"
                sem = asyncio.Semaphore(2)

                async def _do_one_chunk(ci: Dict[str, Any]) -> List[Dict[str, Any]]:
                    async with sem:
                        idx = int(ci["index"])
                        offset_s = float(ci.get("offset_s") or 0.0)
                        p = Path(ci["path"])
                        b = p.read_bytes()
                        ct = "audio/wav" if p.suffix.lower() == ".wav" else "application/octet-stream"

                        log.infox(
                            "voice:process_job:wordts_chunk_start",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            chunk_index=idx,
                            chunk_path=str(p),
                            offset_s=offset_s,
                            bytes_count=len(b),
                            content_type=ct,
                            wordts_model=self.wordts_model,
                        )

                        resp = await self._transcribe_word_timestamps_async(b, filename=p.name, content_type=ct)
                        write_json(chunks_dir / f"wordts_{idx:05d}.json", resp)

                        log.debugx(
                            "voice:process_job:wordts_chunk_response",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            chunk_index=idx,
                            response_keys=list(resp.keys()) if isinstance(resp, dict) else None,
                            raw_word_count=len((resp or {}).get("words") or []) if isinstance(resp, dict) else None,
                        )

                        out: List[Dict[str, Any]] = []
                        for w in (resp.get("words") or []):
                            if not isinstance(w, dict):
                                continue
                            word = (w.get("word") or w.get("text") or "").strip()
                            if not word:
                                continue
                            start = w.get("start") if "start" in w else w.get("start_s")
                            end = w.get("end") if "end" in w else w.get("end_s")
                            try:
                                start_f = float(start) + offset_s
                                end_f = float(end) + offset_s
                            except Exception:
                                continue
                            out.append({"start_s": start_f, "end_s": end_f, "word": word})

                        log.infox(
                            "voice:process_job:wordts_chunk_done",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            chunk_index=idx,
                            parsed_word_count=len(out),
                        )
                        return out

                for i, ci in enumerate(chunks_index):
                    prog = 0.30 + 0.40 * (i / max(1, len(chunks_index)))
                    self._write_status(
                        run_dir,
                        state=self.STATE_TRANSCRIBING,
                        step=f"wordts_chunk_{int(ci['index']):05d}",
                        progress=prog,
                        message=f"Word timestamps chunk {i + 1}/{len(chunks_index)}.",
                        error=None,
                    )
                    words_i = await asyncio.wait_for(_do_one_chunk(ci), timeout=min(remaining_s(), 1800.0))
                    merged_words.extend(words_i)
                    log.debugx(
                        "voice:process_job:wordts_chunk_merged",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        chunk_index=int(ci["index"]),
                        chunk_word_count=len(words_i),
                        merged_word_count=len(merged_words),
                    )

                wordts_merged = {"words": merged_words, "updated_utc": utc_iso(), "chunked": True}
                write_json(run_dir / "wordts.json", wordts_merged)

                log.infox(
                    "voice:process_job:wordts_all_done",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    chunk_count=len(chunks_index),
                    merged_word_count=len(merged_words),
                )

                self._write_status(
                    run_dir,
                    state=self.STATE_ALIGNING,
                    step="build_rich",
                    progress=0.72,
                    message="Building rich transcript (speaker alignment).",
                    error=None,
                )

                log.infox(
                    "voice:process_job:build_rich_start",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                )
                rich = self._build_rich_transcript(diarized, wordts_merged)
                rich_dict = {
                    "text": rich.text,
                    "speakers": rich.speakers,
                    "diarized_segments": [s.__dict__ for s in rich.diarized_segments],
                    "words": [w.__dict__ for w in rich.words],
                }
                write_json(run_dir / "rich.json", rich_dict)

                log.infox(
                    "voice:process_job:build_rich_done",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    transcript_len=len(rich.text or ""),
                    speaker_count=len(rich.speakers),
                    diarized_segment_count=len(rich.diarized_segments),
                    speaker_word_count=len(rich.words),
                )

                transcript = (rich.text or "").strip()
                if not transcript:
                    log.warningx(
                        "voice:process_job:no_speech_detected",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                    )
                    data = self._empty_assistant_payload()
                    markdown = "**No speech detected.**\n\nTry recording again a bit closer to the mic."
                    write_json(run_dir / "assistant_data.json", data)
                    write_text(run_dir / "markdown.md", markdown)
                    self._write_status(run_dir, state=self.STATE_DONE, step="done", progress=1.0, message="Done (no speech).", error=None)
                    return VoiceServiceResult(
                        transcript="",
                        data=data,
                        markdown=markdown,
                        response_id="",
                        raw=None,
                        rich=rich_dict,
                        run_dir=str(run_dir),
                    )

                self._write_status(
                    run_dir,
                    state=self.STATE_EXTRACTING,
                    step="assistant_extract",
                    progress=0.80,
                    message="Extracting meeting intelligence.",
                    error=None,
                )

                assistant = VoiceAssistant()
                diarized_segments_payload = [s.__dict__ for s in rich.diarized_segments]
                speakers_payload = rich.speakers

                log.infox(
                    "voice:process_job:assistant_extract_prepare",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    transcript_len=len(transcript),
                    diarized_segment_count=len(diarized_segments_payload),
                    speaker_count=len(speakers_payload),
                    max_segments_per_assistant_call=self.max_segments_per_assistant_call,
                    assistant_parallelism=self.assistant_parallelism,
                )

                if len(diarized_segments_payload) > self.max_segments_per_assistant_call:
                    partials_dir = run_dir / "partials"
                    partials_dir.mkdir(parents=True, exist_ok=True)

                    slices = self._slice_diarized_segments(diarized_segments_payload, self.max_segments_per_assistant_call)
                    sem_llm = asyncio.Semaphore(self.assistant_parallelism)

                    log.infox(
                        "voice:process_job:assistant_partial_mode",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        slice_count=len(slices),
                        max_segments_per_slice=self.max_segments_per_assistant_call,
                    )

                    async def _extract_one_slice(si: int, segs_slice: List[Dict[str, Any]]) -> Dict[str, Any]:
                        async with sem_llm:
                            log.infox(
                                "voice:process_job:assistant_partial_start",
                                thread_id=thread_id,
                                run_dir=str(run_dir),
                                slice_index=si,
                                segment_count=len(segs_slice),
                            )
                            prompt_text = assistant.prompt(
                                "",
                                transcript="",
                                speakers=speakers_payload,
                                diarized_segments=segs_slice,
                                **payload,
                            )
                            rr: ResponseResult = await asyncio.to_thread(
                                self.responses.ask,
                                [{"role": "user", "content": prompt_text}],
                                session_id=thread_id,
                                keep_context=self.keep_context,
                                model=assistant_model,
                                instructions=assistant.instructions,
                            )
                            write_text(partials_dir / f"assistant_raw_{si:05d}.txt", rr.text)
                            data_any = assistant.extract_first_json_object(rr.text)
                            if not isinstance(data_any, dict):
                                log.warningx(
                                    "voice:process_job:assistant_partial_invalid_json",
                                    thread_id=thread_id,
                                    run_dir=str(run_dir),
                                    slice_index=si,
                                    response_len=len(rr.text or ""),
                                    extracted_type=type(data_any).__name__,
                                )
                                raise ValueError(f"Partial VoiceAssistant returned non-object JSON: {type(data_any)}")
                            write_json(partials_dir / f"partial_{si:05d}.json", data_any)
                            log.infox(
                                "voice:process_job:assistant_partial_done",
                                thread_id=thread_id,
                                run_dir=str(run_dir),
                                slice_index=si,
                                data_keys=list(data_any.keys()),
                            )
                            return data_any

                    partial_results: List[Dict[str, Any]] = []
                    for si, segs_slice in enumerate(slices):
                        prog = 0.80 + 0.10 * (si / max(1, len(slices)))
                        self._write_status(
                            run_dir,
                            state=self.STATE_EXTRACTING,
                            step=f"assistant_partial_{si + 1}/{len(slices)}",
                            progress=prog,
                            message=f"Extracting partial {si + 1}/{len(slices)}.",
                            error=None,
                        )
                        partial_results.append(
                            await asyncio.wait_for(_extract_one_slice(si, segs_slice), timeout=min(remaining_s(), 1800.0))
                        )

                    log.infox(
                        "voice:process_job:assistant_partials_done",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        partial_count=len(partial_results),
                    )

                    merged_partial = self._merge_assistant_partials(partial_results, speakers_payload)
                    write_json(run_dir / "merged_partial.json", merged_partial)

                    log.debugx(
                        "voice:process_job:assistant_partials_merged",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        merged_keys=list(merged_partial.keys()),
                    )

                    self._write_status(
                        run_dir,
                        state=self.STATE_EXTRACTING,
                        step="assistant_synthesis",
                        progress=0.91,
                        message="Final synthesis.",
                        error=None,
                    )

                    synthesis_prompt = self._build_synthesis_prompt(assistant, merged_partial, speakers_payload)

                    log.infox(
                        "voice:process_job:assistant_synthesis_start",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        prompt_len=len(synthesis_prompt),
                    )
                    rr: ResponseResult = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.responses.ask,
                            [{"role": "user", "content": synthesis_prompt}],
                            session_id=thread_id,
                            keep_context=self.keep_context,
                            model=assistant_model,
                            instructions=assistant.instructions,
                        ),
                        timeout=min(remaining_s(), 1800.0),
                    )

                    write_text(run_dir / "assistant_prompt.txt", synthesis_prompt)
                    write_text(run_dir / "assistant_raw.txt", rr.text)

                    data_any = assistant.extract_first_json_object(rr.text)
                    if not isinstance(data_any, dict):
                        log.warningx(
                            "voice:process_job:assistant_synthesis_invalid_json",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            response_len=len(rr.text or ""),
                            extracted_type=type(data_any).__name__,
                        )
                        raise ValueError(f"Synthesis VoiceAssistant returned non-object JSON: {type(data_any)}")
                    data: Dict[str, Any] = data_any
                    log.infox(
                        "voice:process_job:assistant_synthesis_done",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        data_keys=list(data.keys()),
                    )
                else:
                    log.infox(
                        "voice:process_job:assistant_single_call_start",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        transcript_len=len(transcript),
                        diarized_segment_count=len(diarized_segments_payload),
                    )
                    prompt_text = assistant.prompt(
                        transcript,
                        transcript=transcript,
                        speakers=speakers_payload,
                        diarized_segments=diarized_segments_payload,
                        **payload,
                    )
                    write_text(run_dir / "assistant_prompt.txt", prompt_text)

                    rr: ResponseResult = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.responses.ask,
                            [{"role": "user", "content": prompt_text}],
                            session_id=thread_id,
                            keep_context=self.keep_context,
                            model=assistant_model,
                            instructions=assistant.instructions,
                        ),
                        timeout=min(remaining_s(), 1800.0),
                    )
                    write_text(run_dir / "assistant_raw.txt", rr.text)

                    log.infox(
                        "voice:process_job:assistant_single_call_response",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        response_len=len(rr.text or ""),
                    )

                    data_any = assistant.extract_first_json_object(rr.text)
                    if not isinstance(data_any, dict):
                        log.warningx(
                            "voice:process_job:assistant_single_invalid_json",
                            thread_id=thread_id,
                            run_dir=str(run_dir),
                            response_len=len(rr.text or ""),
                            extracted_type=type(data_any).__name__,
                        )
                        raise ValueError(f"VoiceAssistant returned non-object JSON: {type(data_any)}")
                    data = data_any
                    log.infox(
                        "voice:process_job:assistant_single_call_done",
                        thread_id=thread_id,
                        run_dir=str(run_dir),
                        data_keys=list(data.keys()),
                    )

                write_json(run_dir / "assistant_data.json", data)

                self._write_status(run_dir, state=self.STATE_RENDERING, step="render_markdown", progress=0.97, message="Rendering markdown.", error=None)

                # ✅ single source of truth
                log.infox(
                    "voice:process_job:markdown_render_start",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    data_keys=list(data.keys()),
                    transcript_len=len(transcript),
                )
                markdown = default_markdown_service.render(data, mode="final", transcript=transcript)
                write_text(run_dir / "markdown.md", markdown)

                log.infox(
                    "voice:process_job:markdown_render_done",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    markdown_len=len(markdown),
                )

                self._write_status(run_dir, state=self.STATE_DONE, step="done", progress=1.0, message="Done.", error=None)

                await self._maybe_mcp_ingest_final_markdown(
                    run_dir=run_dir,
                    thread_id=thread_id,
                    run_id=run_dir.name,
                    title=self._mcp_title_from_meta(meta, fallback_run_id=run_dir.name),
                    markdown=markdown,
                )

                log.infox(
                    "voice:job_done",
                    run_dir=str(run_dir),
                    transcript_len=len(transcript),
                    md_len=len(markdown),
                    diarized_segments=len(rich.diarized_segments),
                    words=len(rich.words),
                    elapsed_s=round(loop.time() - t0, 2),
                )

                return VoiceServiceResult(
                    transcript=transcript,
                    data=data,
                    markdown=markdown,
                    response_id="",
                    raw=None,
                    rich=rich_dict,
                    run_dir=str(run_dir),
                )

            except Exception as e:
                err = {"error": str(e), "type": type(e).__name__, "updated_utc": utc_iso()}
                write_json(run_dir / "error.json", err)
                self._write_status(run_dir, state=self.STATE_FAILED, step="failed", progress=1.0, message="Failed.", error=err)
                log.exception("voice:job_failed")
                raise
            finally:
                log.debugx(
                    "voice:process_job:release_lock",
                    thread_id=thread_id,
                    run_dir=str(run_dir),
                    lock_path=str(lock) if lock else None,
                )
                self._release_lock(lock)

    # =============================================================
    # MCP ingest
    # =============================================================
    def _mcp_title_from_meta(self, meta: Dict[str, Any], *, fallback_run_id: str) -> str:
        log.debugx(
            "voice:mcp_title_from_meta:start",
            fallback_run_id=fallback_run_id,
            original_filename=meta.get("original_filename") if isinstance(meta, dict) else None,
        )
        fn = (meta.get("original_filename") or "").strip()
        if fn:
            result = f"Voice note: {fn}"
            log.debugx(
                "voice:mcp_title_from_meta:filename",
                title=result,
            )
            return result
        result = f"Voice note: {fallback_run_id}"
        log.debugx(
            "voice:mcp_title_from_meta:fallback",
            title=result,
        )
        return result

    async def _maybe_mcp_ingest_final_markdown(
        self,
        *,
        run_dir: Path,
        thread_id: str,
        run_id: str,
        title: str,
        markdown: str,
    ) -> None:
        log.infox(
            "voice:mcp_ingest:start",
            run_dir=str(run_dir),
            thread_id=thread_id,
            run_id=run_id,
            title=title,
            markdown_len=len(markdown or ""),
            enable_mcp_ingest=self.enable_mcp_ingest,
        )

        if not self.enable_mcp_ingest:
            log.infox(
                "voice:mcp_ingest:skipped_disabled",
                run_dir=str(run_dir),
                thread_id=thread_id,
                run_id=run_id,
            )
            return
        subdir = f"{self.mcp_subdir_prefix}/{safe_slug(thread_id, max_len=80)}"

        try:
            from services.assistants.ask_job_callbacks import text_indexer
            from services.text.text_storage_service import IncomingText

            log.infox(
                "voice:text_ingest_call_start",
                run_dir=str(run_dir),
                thread_id=thread_id,
                run_id=run_id,
                title=title,
                subdir=subdir,
            )
            resp = await asyncio.to_thread(
                text_indexer.ingest_text,
                IncomingText(
                    source="voice",
                    title=title,
                    content=markdown,
                    subdir=subdir,
                ),
            )
            write_json(
                run_dir / "mcp_ingest.json",
                {"ok": True, "request": {"title": title, "subdir": subdir}, "response": resp, "updated_utc": utc_iso()},
            )
            log.infox("voice:text_ingest_done", run_dir=str(run_dir), subdir=subdir, resp=resp)

        except Exception as e:
            write_json(run_dir / "mcp_ingest.json",
                       {"ok": False, "error": str(e), "type": type(e).__name__, "updated_utc": utc_iso()})
            log.exceptionx("voice:text_ingest_failed", run_dir=str(run_dir))

    # =============================================================
    # Polling helpers
    # =============================================================
    def get_voice_job_status(self, *, run_dir: str | Path) -> Dict[str, Any]:
        log.infox(
            "voice:get_status:start",
            run_dir=str(run_dir),
        )

        run_dir = Path(run_dir)
        status = read_json(run_dir / "status.json")
        if isinstance(status, dict):
            log.infox(
                "voice:get_status:found",
                run_dir=str(run_dir),
                state=status.get("state"),
                step=status.get("step"),
                progress=status.get("progress"),
            )
            return status

        log.warningx(
            "voice:get_status:not_found",
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

    def get_voice_job_result(self, *, run_dir: str | Path) -> Dict[str, Any]:
        log.infox(
            "voice:get_result:start",
            run_dir=str(run_dir),
        )

        run_dir = Path(run_dir)
        out: Dict[str, Any] = {"run_dir": str(run_dir), "status": self.get_voice_job_status(run_dir=run_dir), "artifacts": self._artifact_paths(run_dir)}
        for name in ["meta.json", "rich.json", "assistant_data.json", "merged_partial.json", "error.json", "duration.json", "mcp_ingest.json", "mcp_ingest_status.json"]:
            p = run_dir / name
            if p.exists():
                log.debugx(
                    "voice:get_result:read_artifact",
                    run_dir=str(run_dir),
                    artifact=name,
                    path=str(p),
                )
                out[name.replace(".json", "")] = read_json(p)
        md = run_dir / "markdown.md"
        if md.exists():
            out["markdown"] = md.read_text(encoding="utf-8", errors="replace")

        log.infox(
            "voice:get_result:done",
            run_dir=str(run_dir),
            keys=list(out.keys()),
            has_markdown="markdown" in out,
            markdown_len=len(out.get("markdown") or ""),
            artifact_count=len(out.get("artifacts") or {}),
        )
        return out

    async def handle_voice(
        self,
        audio_file: Any,
        *,
        thread_id: str,
        model: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_s: float = 300.0,
    ) -> VoiceServiceResult:
        log.infox(
            "voice:handle_voice:start",
            thread_id=thread_id,
            model=model,
            timeout_s=timeout_s,
            payload_keys=list((payload or {}).keys()),
            audio_type=type(audio_file).__name__,
        )

        seq = StepSequence(log, "voice:handle_voice")
        payload = payload or {}
        with log_context(thread_id=thread_id, model=model):
            with seq.step("start_job"):
                started = await self.start_voice_job(audio_file, thread_id=thread_id, model=model, payload=payload)
            with seq.step("process_job"):
                result = await self.process_voice_job(run_dir=started["run_dir"], model=model, timeout_s=timeout_s)

        log.infox(
            "voice:handle_voice:done",
            thread_id=thread_id,
            model=model,
            run_dir=result.run_dir,
            transcript_len=len(result.transcript or ""),
            markdown_len=len(result.markdown or ""),
        )
        return result

    # =============================================================
    # Chunk splitting (ffmpeg)
    # =============================================================
    def _require_ffmpeg(self) -> None:
        log.debugx(
            "voice:require_ffmpeg",
            ffmpeg=self._ffmpeg,
            ffprobe=self._ffprobe,
        )
        if not self._ffmpeg:
            log.warningx("voice:require_ffmpeg:missing_ffmpeg")
            raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to enable chunk splitting.")
        if not self._ffprobe:
            log.warningx("voice:require_ffmpeg:missing_ffprobe")
            raise RuntimeError("ffprobe not found on PATH. Install ffmpeg/ffprobe to enable duration detection & chunking.")

    def _ffprobe_duration_s(self, audio_path: Path) -> Optional[float]:
        log.debugx(
            "voice:ffprobe_duration:start",
            audio_path=str(audio_path),
            has_ffprobe=bool(self._ffprobe),
        )

        if not self._ffprobe:
            log.debugx(
                "voice:ffprobe_duration:skipped_no_ffprobe",
                audio_path=str(audio_path),
            )
            return None
        try:
            cmd = [
                self._ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
            result = float(out)
            log.debugx(
                "voice:ffprobe_duration:done",
                audio_path=str(audio_path),
                duration_s=result,
            )
            return result
        except Exception:
            log.warningx(
                "voice:ffprobe_duration:failed",
                audio_path=str(audio_path),
            )
            return None

    def _ffmpeg_convert_to_wav(self, src: Path, dst: Path) -> None:
        log.infox(
            "voice:ffmpeg_convert_to_wav:start",
            src=str(src),
            dst=str(dst),
        )
        self._require_ffmpeg()
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._ffmpeg, "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.infox(
            "voice:ffmpeg_convert_to_wav:done",
            src=str(src),
            dst=str(dst),
            dst_exists=dst.exists(),
            dst_bytes=dst.stat().st_size if dst.exists() else 0,
        )

    def _split_audio_into_chunks(self, run_dir: Path, audio_path: Path, *, duration_s: float) -> List[Dict[str, Any]]:
        log.infox(
            "voice:split_audio:start",
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            duration_s=duration_s,
            chunk_seconds=self.chunk_seconds,
            force_chunk_wav=self.force_chunk_wav,
        )

        self._require_ffmpeg()

        chunks_dir = run_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)

        for p in chunks_dir.glob("chunk_*.wav"):
            try:
                log.debugx(
                    "voice:split_audio:remove_old_chunk",
                    path=str(p),
                )
                p.unlink()
            except Exception:
                log.warningx(
                    "voice:split_audio:remove_old_chunk_failed",
                    path=str(p),
                )

        out_pattern = str(chunks_dir / "chunk_%05d.wav") if self.force_chunk_wav else str(chunks_dir / ("chunk_%05d" + (audio_path.suffix or ".bin")))

        if self.force_chunk_wav:
            cmd = [
                self._ffmpeg, "-y", "-i", str(audio_path),
                "-ac", "1", "-ar", "16000",
                "-f", "segment",
                "-segment_time", str(self.chunk_seconds),
                "-reset_timestamps", "1",
                out_pattern,
            ]
        else:
            cmd = [
                self._ffmpeg, "-y", "-i", str(audio_path),
                "-f", "segment",
                "-segment_time", str(self.chunk_seconds),
                "-reset_timestamps", "1",
                out_pattern,
            ]

        log.debugx(
            "voice:split_audio:ffmpeg_call",
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            out_pattern=out_pattern,
            force_chunk_wav=self.force_chunk_wav,
        )
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        chunk_files = sorted(chunks_dir.glob("chunk_*.wav")) if self.force_chunk_wav else sorted(chunks_dir.glob("chunk_*"))
        chunks_index: List[Dict[str, Any]] = []
        for i, p in enumerate(chunk_files):
            chunks_index.append({"index": i, "offset_s": float(i * self.chunk_seconds), "path": str(p), "duration_s": None})

        write_json(chunks_dir / "chunks.json", {"chunks": chunks_index, "updated_utc": utc_iso(), "duration_s": duration_s})

        log.infox(
            "voice:split_audio:done",
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            chunk_count=len(chunks_index),
            chunks_dir=str(chunks_dir),
        )
        return chunks_index

    # =============================================================
    # Assistant chunking + merging (unchanged from your version)
    # =============================================================
    @staticmethod
    def _slice_diarized_segments(segs: List[Dict[str, Any]], max_per_slice: int) -> List[List[Dict[str, Any]]]:
        log.debugx(
            "voice:slice_diarized_segments:start",
            segment_count=len(segs),
            max_per_slice=max_per_slice,
        )
        out: List[List[Dict[str, Any]]] = []
        i = 0
        while i < len(segs):
            out.append(segs[i:i + max_per_slice])
            i += max_per_slice
        log.debugx(
            "voice:slice_diarized_segments:done",
            segment_count=len(segs),
            slice_count=len(out),
            max_per_slice=max_per_slice,
        )
        return out

    @staticmethod
    def _merge_assistant_partials(partials: List[Dict[str, Any]], speakers: List[Dict[str, Any]]) -> Dict[str, Any]:
        log.infox(
            "voice:merge_assistant_partials:start",
            partial_count=len(partials),
            speaker_count=len(speakers),
        )

        def _dedupe_str(items: List[str]) -> List[str]:
            log.debugx(
                "voice:merge_assistant_partials:dedupe_str_start",
                item_count=len(items),
            )
            seen = set()
            out = []
            for x in items:
                x = (x or "").strip()
                if not x:
                    continue
                k = x.lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(x)
            log.debugx(
                "voice:merge_assistant_partials:dedupe_str_done",
                input_count=len(items),
                output_count=len(out),
            )
            return out

        def _dedupe_dict(items: List[Dict[str, Any]], key_fields: List[str]) -> List[Dict[str, Any]]:
            log.debugx(
                "voice:merge_assistant_partials:dedupe_dict_start",
                item_count=len(items),
                key_fields=key_fields,
            )
            seen = set()
            out = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                key = "||".join([(str(it.get(k) or "").strip().lower()) for k in key_fields])
                if not key.strip("|"):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append(it)
            log.debugx(
                "voice:merge_assistant_partials:dedupe_dict_done",
                input_count=len(items),
                output_count=len(out),
                key_fields=key_fields,
            )
            return out

        summaries = []
        execs = []
        detaileds = []
        bullets: List[str] = []
        highlights: List[Dict[str, Any]] = []
        action_items: List[Dict[str, Any]] = []
        decision_log: List[Dict[str, Any]] = []
        notes: List[str] = []
        questions: List[str] = []

        for p in partials:
            summaries.append((p.get("summary") or "").strip())
            views = p.get("views") or {}
            execs.append((views.get("exec") or "").strip())
            detaileds.append((views.get("detailed") or "").strip())
            bullets.extend([b for b in (views.get("bullets") or []) if isinstance(b, str)])
            highlights.extend([h for h in (p.get("highlights") or []) if isinstance(h, dict)])
            action_items.extend([t for t in (p.get("action_items") or []) if isinstance(t, dict)])
            decision_log.extend([d for d in (p.get("decision_log") or []) if isinstance(d, dict)])
            notes.extend([n for n in (p.get("notes") or []) if isinstance(n, str)])
            questions.extend([q for q in (p.get("questions") or []) if isinstance(q, str)])

        merged = {
            "speakers": speakers,
            "summaries": _dedupe_str(summaries),
            "exec_summaries": _dedupe_str(execs),
            "detailed_summaries": _dedupe_str(detaileds),
            "bullets": _dedupe_str(bullets),
            "highlights": _dedupe_dict(highlights, ["type", "title", "start_s", "end_s"]),
            "action_items": _dedupe_dict(action_items, ["task", "owner_speaker_id", "due"]),
            "decision_log": _dedupe_dict(decision_log, ["decision", "start_s", "end_s"]),
            "notes": _dedupe_str(notes),
            "questions": _dedupe_str(questions),
        }

        log.infox(
            "voice:merge_assistant_partials:done",
            partial_count=len(partials),
            summary_count=len(merged["summaries"]),
            bullet_count=len(merged["bullets"]),
            highlight_count=len(merged["highlights"]),
            action_item_count=len(merged["action_items"]),
            decision_count=len(merged["decision_log"]),
            note_count=len(merged["notes"]),
            question_count=len(merged["questions"]),
        )
        return merged

    @staticmethod
    def _build_synthesis_prompt(assistant: VoiceAssistant, merged_partial: Dict[str, Any], speakers: List[Dict[str, Any]]) -> str:
        log.debugx(
            "voice:build_synthesis_prompt:start",
            merged_partial_keys=list(merged_partial.keys()),
            speaker_count=len(speakers),
            assistant_type=type(assistant).__name__,
        )
        ctx = {"speakers": speakers, "merged_partial_keys": list(merged_partial.keys())}
        result = (
            "You are given merged partial meeting intelligence extracted from different parts of a long recording.\n"
            "Your job is to consolidate into ONE final coherent output.\n"
            "Return JSON only.\n"
            "Do NOT invent new facts.\n"
            "Prefer deduplication and clarity.\n\n"
            f"Context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"Merged partial data:\n{json.dumps(merged_partial, ensure_ascii=False)}\n"
        )
        log.debugx(
            "voice:build_synthesis_prompt:done",
            prompt_len=len(result),
        )
        return result

    # =============================================================
    # Persistence helpers
    # =============================================================
    def _make_run_dir(self, *, thread_id: str, original_filename: str) -> Path:
        log.debugx(
            "voice:make_run_dir:start",
            thread_id=thread_id,
            original_filename=original_filename,
            voice_root=str(self.voice_root),
        )
        tid = safe_slug(thread_id, max_len=80)
        stem = safe_slug(Path(original_filename).stem, max_len=40, fallback="file")
        run_id = f"voice_{utc_stamp()}_{stem}_{uuid.uuid4().hex[:8]}"
        run_dir = self.voice_root / tid / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        log.debugx(
            "voice:make_run_dir:done",
            thread_id=thread_id,
            safe_thread_id=tid,
            stem=stem,
            run_id=run_id,
            run_dir=str(run_dir),
            exists=run_dir.exists(),
        )
        return run_dir

    def _write_audio(self, run_dir: Path, audio_bytes: bytes, *, filename: str, content_type: str) -> Path:
        log.debugx(
            "voice:write_audio:start",
            run_dir=str(run_dir),
            filename=filename,
            content_type=content_type,
            bytes_count=len(audio_bytes or b""),
        )
        ext = Path(filename).suffix
        if not ext:
            ct = (content_type or "").lower()
            if "webm" in ct:
                ext = ".webm"
            elif "wav" in ct:
                ext = ".wav"
            elif "mpeg" in ct or "mp3" in ct:
                ext = ".mp3"
            elif "mp4" in ct or "m4a" in ct:
                ext = ".m4a"
            else:
                ext = ".bin"
        audio_path = run_dir / f"audio{ext}"
        audio_path.write_bytes(audio_bytes)
        log.debugx(
            "voice:write_audio:done",
            run_dir=str(run_dir),
            audio_path=str(audio_path),
            ext=ext,
            exists=audio_path.exists(),
            bytes_count=audio_path.stat().st_size if audio_path.exists() else 0,
        )
        return audio_path

    def _artifact_paths(self, run_dir: Path) -> Dict[str, str]:
        log.debugx(
            "voice:artifact_paths:start",
            run_dir=str(run_dir),
        )
        files = [
            "meta.json", "status.json", "duration.json",
            "diarized.json", "wordts.json", "rich.json",
            "assistant_prompt.txt", "assistant_raw.txt", "assistant_data.json", "markdown.md",
            "merged_partial.json", "error.json",
            "mcp_ingest.json", "mcp_ingest_status.json",
        ]
        out: Dict[str, str] = {}
        for f in files:
            p = run_dir / f
            if p.exists():
                out[f] = str(p)
        audio = sorted(run_dir.glob("audio.*"))
        if audio:
            out["audio"] = str(audio[0])
        chunks = run_dir / "chunks"
        if chunks.exists():
            out["chunks_dir"] = str(chunks)
        partials = run_dir / "partials"
        if partials.exists():
            out["partials_dir"] = str(partials)
        log.debugx(
            "voice:artifact_paths:done",
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
            "voice:write_status:start",
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
            "voice:write_status:done",
            run_dir=str(run_dir),
            status_path=str(status_path),
            state=state,
            step=step,
            progress=status["progress"],
            artifact_count=len(status.get("artifacts") or {}),
            has_error=error is not None,
        )

    def _acquire_lock(self, run_dir: Path) -> Optional[Path]:
        log.debugx(
            "voice:acquire_lock:start",
            run_dir=str(run_dir),
        )
        lock_path = run_dir / ".lock"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(utc_iso())
            log.debugx(
                "voice:acquire_lock:done",
                run_dir=str(run_dir),
                lock_path=str(lock_path),
            )
            return lock_path
        except FileExistsError:
            status = read_json(run_dir / "status.json") or {}
            log.warningx(
                "voice:acquire_lock:already_locked",
                run_dir=str(run_dir),
                lock_path=str(lock_path),
                status=status,
            )
            raise RuntimeError(f"Job is already locked/processing. status={status}")

    @staticmethod
    def _release_lock(lock_path: Optional[Path]) -> None:
        log.debugx(
            "voice:release_lock:start",
            lock_path=str(lock_path) if lock_path else None,
        )
        if not lock_path:
            log.debugx("voice:release_lock:skipped_no_lock")
            return
        try:
            lock_path.unlink(missing_ok=True)
            log.debugx(
                "voice:release_lock:done",
                lock_path=str(lock_path),
            )
        except Exception:
            log.warningx(
                "voice:release_lock:failed",
                lock_path=str(lock_path),
            )

    # =============================================================
    # Internals: audio reading
    # =============================================================
    async def _read_audio_bytes(self, audio: Any) -> Tuple[bytes, str, str]:
        log.debugx(
            "voice:read_audio_bytes:start",
            audio_type=type(audio).__name__,
            has_file=hasattr(audio, "file"),
            has_filename=hasattr(audio, "filename"),
        )

        filename = "audio.webm"
        content_type = "audio/webm"

        if isinstance(audio, (bytes, bytearray)):
            result = (bytes(audio), filename, content_type)
            log.debugx(
                "voice:read_audio_bytes:bytes_input",
                filename=filename,
                content_type=content_type,
                bytes_count=len(result[0]),
            )
            return result

        if hasattr(audio, "file") and hasattr(audio, "filename"):
            try:
                audio.file.seek(0)
                log.debugx("voice:read_audio_bytes:file_seek_ok")
            except Exception:
                log.debugx("voice:read_audio_bytes:file_seek_skipped_or_failed")

            read_attr = getattr(audio, "read", None)
            if callable(read_attr):
                audio_bytes = await audio.read()
            else:
                audio_bytes = audio.file.read()

            filename = getattr(audio, "filename", None) or filename
            content_type = getattr(audio, "content_type", None) or content_type
            log.debugx(
                "voice:read_audio_bytes:upload_file_done",
                filename=filename,
                content_type=content_type,
                bytes_count=len(audio_bytes or b""),
            )
            return audio_bytes or b"", filename, content_type

        def _read_sync() -> bytes:
            try:
                audio.seek(0)
            except Exception:
                pass
            return audio.read()

        audio_bytes = await asyncio.to_thread(_read_sync)
        log.debugx(
            "voice:read_audio_bytes:sync_reader_done",
            filename=filename,
            content_type=content_type,
            bytes_count=len(audio_bytes or b""),
        )
        return audio_bytes or b"", filename, content_type

    # =============================================================
    # Internals: OpenAI transcription
    # =============================================================
    @staticmethod
    def _resolve_chat_model() -> Optional[str]:
        """First assigned chat-slot model for the voice assistant response. None
        when no chat slot is configured (the responses layer then has no model)."""
        from db.database import SessionLocal
        from services.providers.provider_factory import resolve_default_chat_model
        db = SessionLocal()
        try:
            return resolve_default_chat_model(db)
        except Exception as exc:  # noqa: BLE001 — never break the voice path
            log.warningx("voice:resolve_chat_model:failed", error=str(exc))
            return None
        finally:
            db.close()

    def _resolve_transcription_model(self, explicit: Optional[str] = None) -> str:
        """Effective transcription model: an explicit value, else the OpenAI-backed
        'transcription' slot. Raises when nothing is configured so the OpenAI-only
        diarization/word-timestamp features gate off cleanly (no hardcoded model)."""
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
                "Recordings (STT) is not configured. Assign an OpenAI transcription "
                "model to the Recordings (STT) slot under AI Models → Routing."
            )
        return mid

    async def _transcribe_diarized_async(self, audio_path: Path, *, content_type: str) -> Dict[str, Any]:
        use_model = self._resolve_transcription_model(self.diarize_model)
        log.infox(
            "voice:transcribe_diarized:start",
            audio_path=str(audio_path),
            content_type=content_type,
            model=use_model,
            audio_exists=audio_path.exists(),
            audio_bytes=audio_path.stat().st_size if audio_path.exists() else 0,
        )

        def _call() -> Any:
            with audio_path.open("rb") as f:
                return self.client.audio.transcriptions.create(
                    model=use_model,
                    file=(audio_path.name, f.read(), content_type),
                    response_format="diarized_json",
                    chunking_strategy="auto",
                )

        resp = await asyncio.to_thread(_call)
        if isinstance(resp, dict):
            result = resp
        elif hasattr(resp, "model_dump"):
            result = resp.model_dump()
        else:
            result = json.loads(json.dumps(resp, default=lambda o: getattr(o, "__dict__", str(o))))

        log.infox(
            "voice:transcribe_diarized:done",
            audio_path=str(audio_path),
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )
        return result

    async def _transcribe_word_timestamps_async(self, audio_bytes: bytes, *, filename: str, content_type: str) -> Dict[str, Any]:
        use_model = self._resolve_transcription_model(self.wordts_model)
        log.infox(
            "voice:transcribe_word_timestamps:start",
            filename=filename,
            content_type=content_type,
            model=use_model,
            bytes_count=len(audio_bytes or b""),
        )

        def _call() -> Any:
            return self.client.audio.transcriptions.create(
                model=use_model,
                file=(filename, audio_bytes, content_type),
                response_format="verbose_json",
                timestamp_granularities=["segment", "word"],
            )

        resp = await asyncio.to_thread(_call)
        if isinstance(resp, dict):
            result = resp
        elif hasattr(resp, "model_dump"):
            result = resp.model_dump()
        else:
            result = json.loads(json.dumps(resp, default=lambda o: getattr(o, "__dict__", str(o))))

        log.infox(
            "voice:transcribe_word_timestamps:done",
            filename=filename,
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
            word_count=len((result or {}).get("words") or []) if isinstance(result, dict) else None,
        )
        return result

    # =============================================================
    # Internals: alignment + rich transcript
    # =============================================================
    @staticmethod
    def _extract_diarized_segments(diarized: Dict[str, Any]) -> List[DiarizedSegment]:
        log.debugx(
            "voice:extract_diarized_segments:start",
            diarized_keys=list(diarized.keys()) if isinstance(diarized, dict) else None,
        )
        segs = diarized.get("segments") or diarized.get("speaker_segments") or diarized.get("utterances") or []
        out: List[DiarizedSegment] = []
        for s in segs:
            if not isinstance(s, dict):
                continue
            start = s.get("start") if "start" in s else s.get("start_s")
            end = s.get("end") if "end" in s else s.get("end_s")
            speaker = s.get("speaker") or s.get("speaker_id") or s.get("speaker_label") or "S?"
            text = (s.get("text") or "").strip()
            try:
                start_f = float(start)
                end_f = float(end)
            except Exception:
                continue
            if not text:
                continue
            out.append(DiarizedSegment(start_s=start_f, end_s=end_f, speaker=str(speaker), text=text))
        log.debugx(
            "voice:extract_diarized_segments:done",
            input_segment_count=len(segs) if isinstance(segs, list) else None,
            output_segment_count=len(out),
        )
        return out

    @staticmethod
    def _extract_word_timestamps(wordts: Dict[str, Any]) -> List[WordTS]:
        log.debugx(
            "voice:extract_word_timestamps:start",
            wordts_keys=list(wordts.keys()) if isinstance(wordts, dict) else None,
        )
        words = wordts.get("words") or []
        out: List[WordTS] = []
        for w in words:
            if not isinstance(w, dict):
                continue
            word = (w.get("word") or w.get("text") or "").strip()
            start = w.get("start") if "start" in w else w.get("start_s")
            end = w.get("end") if "end" in w else w.get("end_s")
            if not word:
                continue
            try:
                start_f = float(start)
                end_f = float(end)
            except Exception:
                continue
            out.append(WordTS(start_s=start_f, end_s=end_f, word=word))
        log.debugx(
            "voice:extract_word_timestamps:done",
            input_word_count=len(words) if isinstance(words, list) else None,
            output_word_count=len(out),
        )
        return out

    @staticmethod
    def _build_speakers_list(diarized_segments: List[DiarizedSegment]) -> List[Dict[str, Any]]:
        log.debugx(
            "voice:build_speakers_list:start",
            diarized_segment_count=len(diarized_segments),
        )
        seen: Dict[str, None] = {}
        for s in diarized_segments:
            seen.setdefault(s.speaker, None)
        result = [{"id": k, "name": None} for k in sorted(seen.keys())]
        log.debugx(
            "voice:build_speakers_list:done",
            speaker_count=len(result),
            speaker_ids=[s["id"] for s in result],
        )
        return result

    @staticmethod
    def _assign_speaker_for_word(word: WordTS, diarized_segments: List[DiarizedSegment]) -> str:
        log.debugx(
            "voice:assign_speaker_for_word:start",
            word=word.word,
            start_s=word.start_s,
            end_s=word.end_s,
            diarized_segment_count=len(diarized_segments),
        )
        best_speaker = "S?"
        best_overlap = 0.0
        ws, we = word.start_s, word.end_s
        for seg in diarized_segments:
            ss, se = seg.start_s, seg.end_s
            overlap = max(0.0, min(we, se) - max(ws, ss))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = seg.speaker
        if best_overlap > 0:
            log.debugx(
                "voice:assign_speaker_for_word:overlap_match",
                word=word.word,
                speaker=best_speaker,
                best_overlap=best_overlap,
            )
            return best_speaker

        mid = (ws + we) / 2.0
        best_dist = float("inf")
        for seg in diarized_segments:
            seg_mid = (seg.start_s + seg.end_s) / 2.0
            dist = abs(mid - seg_mid)
            if dist < best_dist:
                best_dist = dist
                best_speaker = seg.speaker
        log.debugx(
            "voice:assign_speaker_for_word:nearest_match",
            word=word.word,
            speaker=best_speaker,
            best_dist=best_dist,
        )
        return best_speaker

    @classmethod
    def _build_rich_transcript(cls, diarized: Dict[str, Any], wordts: Dict[str, Any]) -> RichTranscript:
        log.infox(
            "voice:build_rich_transcript:start",
            diarized_keys=list(diarized.keys()) if isinstance(diarized, dict) else None,
            wordts_keys=list(wordts.keys()) if isinstance(wordts, dict) else None,
        )
        diarized_segments = cls._extract_diarized_segments(diarized)
        speakers = cls._build_speakers_list(diarized_segments)
        words = cls._extract_word_timestamps(wordts)

        speaker_words: List[SpeakerWord] = []
        for w in words:
            spk = cls._assign_speaker_for_word(w, diarized_segments) if diarized_segments else "S?"
            speaker_words.append(SpeakerWord(start_s=w.start_s, end_s=w.end_s, speaker=spk, word=w.word))

        if diarized_segments:
            text = "\n".join([f"{s.speaker}: {s.text}" for s in diarized_segments]).strip()
        else:
            text = " ".join([w.word for w in speaker_words]).strip()

        result = RichTranscript(text=text, diarized_segments=diarized_segments, words=speaker_words, speakers=speakers)
        log.infox(
            "voice:build_rich_transcript:done",
            transcript_len=len(result.text or ""),
            diarized_segment_count=len(result.diarized_segments),
            word_count=len(result.words),
            speaker_count=len(result.speakers),
        )
        return result

    # =============================================================
    # Defaults
    # =============================================================
    @staticmethod
    def _empty_assistant_payload() -> Dict[str, Any]:
        log.debugx("voice:empty_assistant_payload:create")
        return {
            "summary": "",
            "views": {"exec": "", "detailed": "", "bullets": []},
            "speakers": [],
            "highlights": [],
            "action_items": [],
            "decision_log": [],
            "mind_map": {"format": "mermaid", "content": "mindmap\n  root((No speech))"},
            "notes": ["No speech detected in recording."],
            "questions": ["Could you try again closer to the mic or in a quieter room?"],
        }

    # =============================================================
    # Optional helper (kept from your file)
    # =============================================================
    async def transcribe_to_text(self, audio_file: Any, *, model: Optional[str] = None) -> str:
        log.infox(
            "voice:transcribe_to_text:start",
            requested_model=model,
            audio_type=type(audio_file).__name__,
        )

        audio_bytes, filename, content_type = await self._read_audio_bytes(audio_file)
        if not audio_bytes:
            log.warningx(
                "voice:transcribe_to_text:empty_audio",
                filename=filename,
                content_type=content_type,
            )
            return ""

        use_model = self._resolve_transcription_model(model)

        log.infox(
            "voice:transcribe_to_text:openai_call_start",
            filename=filename,
            content_type=content_type,
            bytes_count=len(audio_bytes),
            model=use_model,
        )

        def _call() -> Any:
            return self.client.audio.transcriptions.create(
                model=use_model,
                file=(filename, audio_bytes, content_type),
                response_format="text",
            )

        resp = await asyncio.to_thread(_call)

        if isinstance(resp, str):
            result = resp.strip()
            log.infox(
                "voice:transcribe_to_text:done_str",
                text_len=len(result),
            )
            return result
        if isinstance(resp, dict):
            result = str(resp.get("text") or "").strip()
            log.infox(
                "voice:transcribe_to_text:done_dict",
                text_len=len(result),
            )
            return result
        if hasattr(resp, "text"):
            result = str(getattr(resp, "text") or "").strip()
            log.infox(
                "voice:transcribe_to_text:done_text_attr",
                text_len=len(result),
            )
            return result

        result = str(resp).strip()
        log.infox(
            "voice:transcribe_to_text:done_fallback",
            text_len=len(result),
            response_type=type(resp).__name__,
        )
        return result