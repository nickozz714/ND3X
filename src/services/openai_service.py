"""
responses_service.py
Instrumented with dynamic logging:
- per-call StepSequence timing (duration_ms, since_prev_ms)
- context fields: model, batch_size, knobs, vec dims, file counts, etc.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union, Iterable
from collections import defaultdict
import asyncio
import math

from openai import OpenAI, AsyncOpenAI

# --- logging imports & setup ---
from component.logging import (
    get_logger, StepSequence, new_trace_id, log_context
)
from component.config import settings

from services.openai_usage_control import (
    estimate_openai_request_usage,
    is_context_length_error,
    is_rate_limit_error,
    sleep_for_rate_limit_retry,
    GLOBAL_OPENAI_RATE_LIMITER,
)
log = get_logger("svc.responses")


# -----------------------------
# Public result container
# -----------------------------

@dataclass
class ResponseResult:
    text: str
    response_id: str
    raw: Any  # Full SDK object


# -----------------------------
# Core service
# -----------------------------

class OpenAIResponsesService:
    """
    Dynamic service for Responses API + Embeddings + Vector Stores.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Optional[str] = None,  # no hardcoded default; chat model comes from slots
        instructions: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        tool_resources: Optional[Dict[str, Any]] = None,
        store: bool = True,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        embedding_model: Optional[str] = None,  # no hardcoded default; embeddings come from the slot
        transcription_model: Optional[str] = None,  # transcription comes from the transcription slot
        voice_action_model: Optional[str] = None,
        client: Optional[OpenAI] = None,
        api_key_provider: Optional[Any] = None,
    ):
        # No global OpenAI key: when neither an explicit key nor a provider is
        # given, resolve it lazily from the registry's OpenAI provider — so OpenAI
        # auth is only needed once an OpenAI model is actually called.
        if api_key is None and api_key_provider is None:
            from services.providers.openai_key import registry_openai_api_key
            api_key_provider = registry_openai_api_key
        log.infox(
            "responses:init_start",
            model=model,
            embedding_model=embedding_model,
            transcription_model=transcription_model,
            voice_action_model=voice_action_model or model,
            has_api_key=bool(api_key) or api_key_provider is not None,
            has_client=client is not None,
            has_instructions=instructions is not None,
            instruction_len=len(instructions or ""),
            tool_count=len(tools or []),
            has_tool_choice=tool_choice is not None,
            has_tool_resources=bool(tool_resources),
            store=bool(store),
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            metadata_keys=list((metadata or {}).keys()),
        )

        seq = StepSequence(log, "responses:init")
        with seq.step("create_client"), log_context(model=model, embed_model=embedding_model, store=bool(store)):
            # Clients are built lazily (on first use) so the OpenAI key is resolved
            # from the registry only when an OpenAI model is actually called.
            self._api_key = api_key
            self._api_key_provider = api_key_provider
            self._injected_client = client
            self._sync_client: Optional[OpenAI] = None
            self._async_client: Optional[AsyncOpenAI] = None

            # Defaults (overridable per call)
            self.default_model = model
            self.default_instructions = instructions
            self.default_tools = tools or []
            self.default_tool_choice = tool_choice or []
            self.default_tool_resources = tool_resources or {}
            self.default_store = bool(store)
            self.default_temperature = temperature
            self.default_top_p = top_p
            self.default_max_output_tokens = max_output_tokens
            self.default_metadata = metadata or {}

            # Embeddings
            self.default_embedding_model = embedding_model

            self.default_transcription_model = transcription_model
            self.default_voice_action_model = voice_action_model or model

            # In-memory session map: session_id -> last response.id
            self._last_id_by_session: Dict[str, str] = {}

            # Prevent overlapping Responses API calls from racing the same session context.
            # Different sessions can still run concurrently.
            self._session_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
            self.rate_limiter = GLOBAL_OPENAI_RATE_LIMITER

            log.debugx(
                "responses:defaults_set",
                default_model=self.default_model,
                default_embedding_model=self.default_embedding_model,
                default_transcription_model=self.default_transcription_model,
                default_voice_action_model=self.default_voice_action_model,
                default_tool_count=len(self.default_tools or []),
                default_tool_resource_keys=list((self.default_tool_resources or {}).keys()),
                default_store=self.default_store,
                default_temperature=self.default_temperature,
                default_top_p=self.default_top_p,
                default_max_output_tokens=self.default_max_output_tokens,
                default_metadata_keys=list((self.default_metadata or {}).keys()),
            )

        log.infox("responses:service_ready")

    # ----------------- Lazy OpenAI clients (registry-keyed) -----------------
    def _resolve_api_key(self) -> Optional[str]:
        if self._api_key:
            return self._api_key
        if self._api_key_provider is not None:
            return self._api_key_provider()
        return None

    @property
    def client(self) -> OpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self._sync_client is None:
            self._sync_client = OpenAI(api_key=self._resolve_api_key(), timeout=60 * 60)
        return self._sync_client

    @property
    def async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(api_key=self._resolve_api_key(), timeout=60 * 60)
        return self._async_client

    def reset_thread_sessions(self, thread_id: str) -> int:
        """Drop the server-side Responses chain (previous_response_id) for every
        session belonging to a thread, so subsequent calls start fresh instead of
        carrying the full accumulated context. Used by compaction. Returns the
        number of session chains cleared."""
        if not thread_id:
            return 0
        keys = [k for k in list(self._last_id_by_session.keys()) if thread_id in str(k)]
        for k in keys:
            self._last_id_by_session.pop(k, None)
        return len(keys)

    @staticmethod
    def _record_response_usage(resp: Any, *, model: Optional[str] = None, role: Optional[str] = None) -> None:
        """Push actual token usage from a Responses API result to the per-request
        accumulator (no-op outside a collecting context)."""
        try:
            from services.providers.usage_accumulator import add as _usage_add
            u = getattr(resp, "usage", None)
            if u is None:
                return
            inp = getattr(u, "input_tokens", None)
            if inp is None:
                inp = getattr(u, "prompt_tokens", 0)
            out = getattr(u, "output_tokens", None)
            if out is None:
                out = getattr(u, "completion_tokens", 0)
            _usage_add(input_tokens=inp, output_tokens=out, model=model, provider_type="openai", role=role)
        except Exception:  # noqa: BLE001 — usage accounting must never break a call
            pass

    # ----------------- High-level Chat API -----------------
    def resolves_to_openai(self, model: Optional[str] = None, role: Optional[str] = None) -> bool:
        """This service IS the OpenAI base path — the planner JSON streams as free text."""
        return True

    def supports_server_side_session(self, model: Optional[str] = None, role: Optional[str] = None) -> bool:
        """This service IS the OpenAI Responses path: it always has a server-side session
        (previous_response_id chaining per session_id). Mirrors LLMRouter's probe so callers
        can decide whether to send only the delta vs. the full accumulated context."""
        return True

    async def ask_orchestration_async(
            self,
            user_input: Union[str, List[Dict[str, str]]],
            *,
            role: str,
            session_id: Optional[str] = None,
            keep_context: bool = False,
            model: Optional[str] = None,
            instructions: Optional[str] = None,
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Optional[str] = None,
            tool_resources: Optional[Dict[str, Any]] = None,
            store: Optional[bool] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_output_tokens: Optional[int] = None,
            response_format: Optional[Dict[str, Any]] = None,
            metadata: Optional[Dict[str, str]] = None,
    ) -> ResponseResult:
        """
        Orchestration-safe wrapper.

        Policy:
        - If keep_context=True, use the provided session_id exactly.
        - If keep_context=False, scope the session id by role and never attach previous_response_id.
        - Defaults store=False for isolated orchestration calls.
        """
        scoped_session_id = session_id

        if session_id and not keep_context:
            scoped_session_id = f"{session_id}:{role}"

        final_metadata = dict(metadata or {})
        final_metadata["orchestration_role"] = role
        final_metadata["keep_context"] = str(bool(keep_context)).lower()

        return await self.ask_async(
            user_input,
            session_id=scoped_session_id,
            keep_context=keep_context,
            model=model,
            instructions=instructions,
            tools=tools,
            tool_choice=tool_choice,
            tool_resources=tool_resources,
            store=bool(store) if store is not None else bool(keep_context),
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
            metadata=final_metadata,
            previous_response_id=None,
        )

    async def ask_async(
            self,
            user_input: Union[str, List[Dict[str, str]]],
            *,
            session_id: Optional[str] = None,
            keep_context: bool = False,
            model: Optional[str] = None,
            instructions: Optional[str] = None,
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Optional[str] = None,
            tool_resources: Optional[Dict[str, Any]] = None,
            store: Optional[bool] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_output_tokens: Optional[int] = None,
            response_format: Optional[Dict[str, Any]] = None,
            metadata: Optional[Dict[str, str]] = None,
            previous_response_id: Optional[str] = None,
    ) -> ResponseResult:
        """
        Async Responses API call.

        Important:
        - Different sessions can run concurrently.
        - Calls that use session context are serialized per session_id so
          _last_id_by_session cannot race/fork the conversation chain.
        """
        log.infox(
            "responses:ask_async_entry",
            session_id=session_id,
            keep_context=bool(keep_context),
            model=model or self.default_model,
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            has_instructions=instructions is not None,
            tool_count=len(tools or []) if tools is not None else len(self.default_tools or []),
            has_tool_choice=tool_choice is not None,
            has_tool_resources=bool(tool_resources),
            store=self.default_store if store is None else bool(store),
            temperature=temperature if temperature is not None else self.default_temperature,
            top_p=top_p if top_p is not None else self.default_top_p,
            max_output_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
            has_response_format=response_format is not None,
            metadata_keys=list((metadata or {}).keys()),
            has_previous_response_id=bool(previous_response_id),
        )

        should_lock_session = bool(session_id and (keep_context or previous_response_id))

        log.debugx(
            "responses:ask_async_lock_decision",
            session_id=session_id,
            should_lock_session=should_lock_session,
            keep_context=bool(keep_context),
            has_previous_response_id=bool(previous_response_id),
            known_session_count=len(self._last_id_by_session),
            lock_count=len(self._session_locks),
        )

        if should_lock_session:
            log.debugx(
                "responses:ask_async_waiting_for_session_lock",
                session_id=session_id,
            )
            async with self._session_locks[str(session_id)]:
                log.debugx(
                    "responses:ask_async_session_lock_acquired",
                    session_id=session_id,
                    last_response_id=self._last_id_by_session.get(str(session_id)),
                )
                return await self._ask_async_inner(
                    user_input=user_input,
                    session_id=session_id,
                    keep_context=keep_context,
                    model=model,
                    instructions=instructions,
                    tools=tools,
                    tool_choice=tool_choice,
                    tool_resources=tool_resources,
                    store=store,
                    temperature=temperature,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                    response_format=response_format,
                    metadata=metadata,
                    previous_response_id=previous_response_id,
                )

        return await self._ask_async_inner(
            user_input=user_input,
            session_id=session_id,
            keep_context=keep_context,
            model=model,
            instructions=instructions,
            tools=tools,
            tool_choice=tool_choice,
            tool_resources=tool_resources,
            store=store,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
            metadata=metadata,
            previous_response_id=previous_response_id,
        )

    async def _ask_async_inner(
            self,
            user_input: Union[str, List[Dict[str, str]]],
            *,
            session_id: Optional[str] = None,
            keep_context: bool = False,
            model: Optional[str] = None,
            instructions: Optional[str] = None,
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Optional[str] = None,
            tool_resources: Optional[Dict[str, Any]] = None,
            store: Optional[bool] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_output_tokens: Optional[int] = None,
            response_format: Optional[Dict[str, Any]] = None,
            metadata: Optional[Dict[str, str]] = None,
            previous_response_id: Optional[str] = None,
    ) -> ResponseResult:
        seq = StepSequence(log, "responses:ask_async")
        trace_id = new_trace_id()

        log.infox(
            "responses:ask_async_inner_start",
            trace_id=trace_id,
            session_id=session_id,
            keep_context=bool(keep_context),
            model=model or self.default_model,
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            has_previous_response_id=bool(previous_response_id),
        )

        with log_context(
                trace_id=trace_id,
                model=model or self.default_model,
                has_session=bool(session_id),
                session_id=session_id,
                keep_context=bool(keep_context),
                has_prev_id=bool(previous_response_id),
                temperature=temperature if temperature is not None else self.default_temperature,
                top_p=top_p if top_p is not None else self.default_top_p,
                max_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
        ):
            with seq.step("build_request"):
                req = self._build_request(
                    user_input=user_input,
                    model=model,
                    instructions=instructions,
                    tools=tools,
                    tool_choice=tool_choice,
                    tool_resources=tool_resources,
                    store=store,
                    temperature=temperature,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                    response_format=response_format,
                    metadata=metadata,
                )

                prev_id = (
                        previous_response_id
                        or (self._last_id_by_session.get(session_id) if (keep_context and session_id) else None)
                )
                if prev_id:
                    req["previous_response_id"] = prev_id

                usage_estimate = estimate_openai_request_usage(
                    req=req,
                    max_input_tokens=int(getattr(settings, "LLM_MAX_INPUT_TOKENS", 90000)),
                    default_reserved_output_tokens=int(getattr(settings, "LLM_RESERVED_OUTPUT_TOKENS", 8000)),
                )

                log.infox(
                    "responses:usage_estimate",
                    trace_id=trace_id,
                    session_id=session_id,
                    model=req.get("model"),
                    estimated_input_tokens=usage_estimate.input_tokens,
                    reserved_output_tokens=usage_estimate.reserved_output_tokens,
                    total_reserved_tokens=usage_estimate.total_reserved_tokens,
                    max_input_tokens=usage_estimate.max_input_tokens,
                    input_budget_ratio=round(usage_estimate.input_budget_ratio, 4),
                    near_context_limit=usage_estimate.near_context_limit,
                    has_previous_response_id=bool(req.get("previous_response_id")),
                )

                if usage_estimate.near_context_limit:
                    log.warningx(
                        "responses:near_context_limit",
                        trace_id=trace_id,
                        session_id=session_id,
                        model=req.get("model"),
                        estimated_input_tokens=usage_estimate.input_tokens,
                        max_input_tokens=usage_estimate.max_input_tokens,
                        input_budget_ratio=round(usage_estimate.input_budget_ratio, 4),
                        input_len=len(str(req.get("input") or "")),
                        instruction_len=len(str(req.get("instructions") or "")),
                        has_previous_response_id=bool(req.get("previous_response_id")),
                    )

                if usage_estimate.input_tokens > usage_estimate.max_input_tokens:
                    log.errorx(
                        "responses:input_too_large_before_api_call",
                        trace_id=trace_id,
                        session_id=session_id,
                        model=req.get("model"),
                        estimated_input_tokens=usage_estimate.input_tokens,
                        max_input_tokens=usage_estimate.max_input_tokens,
                        input_budget_ratio=round(usage_estimate.input_budget_ratio, 4),
                        has_previous_response_id=bool(req.get("previous_response_id")),
                    )
                    raise ValueError(
                        "OpenAI input too large before API call. "
                        f"Estimated {usage_estimate.input_tokens} input tokens, "
                        f"limit {usage_estimate.max_input_tokens}. "
                        "Start a new session, compact the payload, or isolate this orchestration role."
                    )

                await self.rate_limiter.acquire(
                    model=req.get("model") or self.default_model,
                    estimated_tokens=usage_estimate.total_reserved_tokens,
                )
                log.debugx(
                    "responses:ask_async_request_ready",
                    trace_id=trace_id,
                    session_id=session_id,
                    request_keys=list(req.keys()),
                    model=req.get("model"),
                    store=req.get("store"),
                    has_previous_response_id=bool(req.get("previous_response_id")),
                    previous_response_id=req.get("previous_response_id"),
                    has_tools=bool(req.get("tools")),
                    tool_count=len(req.get("tools") or []),
                    has_tool_resources=bool(req.get("tool_resources")),
                    has_metadata=bool(req.get("metadata")),
                    metadata_keys=list((req.get("metadata") or {}).keys()),
                    has_response_format=bool(req.get("response_format")),
                )

            log.infox(
                "responses:ask_async_start",
                trace_id=trace_id,
                session_id=session_id,
                keep_context=bool(keep_context),
                previous_response_id=prev_id,
                model=req.get("model"),
                input_len=len(str(user_input or "")),
                has_tools=bool(req.get("tools")),
            )

            with seq.step("api_call"):
                max_attempts = int(getattr(settings, "OPENAI_RATE_LIMIT_RETRIES", 3))
                default_wait_s = float(getattr(settings, "OPENAI_RATE_LIMIT_DEFAULT_WAIT_S", 60.0))
                max_wait_s = float(getattr(settings, "OPENAI_RATE_LIMIT_MAX_WAIT_S", 90.0))

                for attempt in range(1, max_attempts + 1):
                    try:
                        resp = await self.async_client.responses.create(**req)
                        self._record_response_usage(
                            resp, model=req.get("model"),
                            role=(req.get("metadata") or {}).get("orchestration_role"),
                        )
                        break

                    except Exception as e:
                        if is_context_length_error(e):
                            log.errorx(
                                "responses:context_length_error",
                                trace_id=trace_id,
                                session_id=session_id,
                                model=req.get("model"),
                                attempt=attempt,
                                estimated_input_tokens=usage_estimate.input_tokens,
                                max_input_tokens=usage_estimate.max_input_tokens,
                                has_previous_response_id=bool(req.get("previous_response_id")),
                                error_type=type(e).__name__,
                                error=str(e),
                            )
                            raise

                        if is_rate_limit_error(e) and attempt < max_attempts:
                            waited_s = await sleep_for_rate_limit_retry(
                                exc=e,
                                attempt=attempt,
                                default_wait_s=default_wait_s,
                                max_wait_s=max_wait_s,
                            )

                            log.warningx(
                                "responses:rate_limit_retry",
                                trace_id=trace_id,
                                session_id=session_id,
                                model=req.get("model"),
                                attempt=attempt,
                                max_attempts=max_attempts,
                                waited_s=round(waited_s, 2),
                                estimated_input_tokens=usage_estimate.input_tokens,
                                reserved_output_tokens=usage_estimate.reserved_output_tokens,
                                error_type=type(e).__name__,
                                error=str(e)[:1000],
                            )
                            continue

                        log.errorx(
                            "responses:api_error",
                            trace_id=trace_id,
                            session_id=session_id,
                            model=req.get("model"),
                            attempt=attempt,
                            max_attempts=max_attempts,
                            estimated_input_tokens=usage_estimate.input_tokens,
                            reserved_output_tokens=usage_estimate.reserved_output_tokens,
                            has_previous_response_id=bool(req.get("previous_response_id")),
                            error_type=type(e).__name__,
                            error=str(e),
                        )
                        raise

            log.infox(
                "responses:ask_async_api_response",
                trace_id=trace_id,
                session_id=session_id,
                response_id=getattr(resp, "id", None),
                response_type=type(resp).__name__,
                has_output_text=bool(getattr(resp, "output_text", None)),
                output_item_count=len(getattr(resp, "output", []) or []),
            )

            with seq.step("extract_text"):
                text = self._extract_text(resp)

            if session_id and (keep_context or previous_response_id):
                self._last_id_by_session[session_id] = resp.id
                log.debugx(
                    "responses:ask_async_session_handle_updated",
                    trace_id=trace_id,
                    session_id=session_id,
                    response_id=resp.id,
                    known_session_count=len(self._last_id_by_session),
                )

            log.infox(
                "responses:ask_async_done",
                trace_id=trace_id,
                session_id=session_id,
                response_id=resp.id,
                text_len=len(text or ""),
            )

            return ResponseResult(text=text, response_id=resp.id, raw=resp)

    def ask(
        self,
        user_input: Union[str, List[Dict[str, str]]],
        *,
        session_id: Optional[str] = None,
        keep_context: bool = False,
        # per-call overrides
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        tool_resources: Optional[Dict[str, Any]] = None,
        store: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, str]] = None,
        previous_response_id: Optional[str] = None,
    ) -> ResponseResult:
        log.infox(
            "responses:ask_start",
            session_id=session_id,
            keep_context=bool(keep_context),
            model=model or self.default_model,
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            has_instructions=instructions is not None,
            tool_count=len(tools or []) if tools is not None else len(self.default_tools or []),
            has_tool_choice=tool_choice is not None,
            has_tool_resources=bool(tool_resources),
            store=self.default_store if store is None else bool(store),
            temperature=temperature if temperature is not None else self.default_temperature,
            top_p=top_p if top_p is not None else self.default_top_p,
            max_output_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
            has_response_format=response_format is not None,
            metadata_keys=list((metadata or {}).keys()),
            has_previous_response_id=bool(previous_response_id),
        )

        seq = StepSequence(log, "responses:ask")
        with log_context(
            model=model or self.default_model,
            has_session=bool(session_id),
            keep_context=bool(keep_context),
            has_prev_id=bool(previous_response_id),
            temperature=temperature if temperature is not None else self.default_temperature,
            top_p=top_p if top_p is not None else self.default_top_p,
            max_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
        ):
            with seq.step("build_request"):
                req = self._build_request(
                    user_input=user_input,
                    model=model,
                    instructions=instructions,
                    tools=tools,
                    tool_choice = tool_choice,
                    tool_resources=tool_resources,
                    store=store,
                    temperature=temperature,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                    response_format=response_format,
                    metadata=metadata,
                )

                prev_id = (
                    previous_response_id
                    or (self._last_id_by_session.get(session_id) if (keep_context and session_id) else None)
                )
                if prev_id:
                    req["previous_response_id"] = prev_id

                log.debugx(
                    "responses:ask_request_ready",
                    session_id=session_id,
                    request_keys=list(req.keys()),
                    model=req.get("model"),
                    store=req.get("store"),
                    has_previous_response_id=bool(req.get("previous_response_id")),
                    previous_response_id=req.get("previous_response_id"),
                    has_tools=bool(req.get("tools")),
                    tool_count=len(req.get("tools") or []),
                    has_tool_resources=bool(req.get("tool_resources")),
                    has_metadata=bool(req.get("metadata")),
                    metadata_keys=list((req.get("metadata") or {}).keys()),
                    has_response_format=bool(req.get("response_format")),
                )

            with seq.step("api_call"):
                resp = self.client.responses.create(**req)
                self._record_response_usage(
                    resp, model=req.get("model"),
                    role=(req.get("metadata") or {}).get("orchestration_role"),
                )

            log.infox(
                "responses:ask_api_response",
                session_id=session_id,
                response_id=getattr(resp, "id", None),
                response_type=type(resp).__name__,
                has_output_text=bool(getattr(resp, "output_text", None)),
                output_item_count=len(getattr(resp, "output", []) or []),
            )

            with seq.step("extract_text"):
                text = self._extract_text(resp)

            if session_id and (keep_context or previous_response_id):
                self._last_id_by_session[session_id] = resp.id
                log.debugx(
                    "responses:ask_session_handle_updated",
                    session_id=session_id,
                    response_id=resp.id,
                    known_session_count=len(self._last_id_by_session),
                )

            log.infox("responses:ask_done", response_id=resp.id, text_len=len(text or ""))
            return ResponseResult(text=text, response_id=resp.id, raw=resp)

    def ask_stream(
        self,
        user_input: Union[str, List[Dict[str, str]]],
        *,
        session_id: Optional[str] = None,
        keep_context: bool = False,
        # per-call overrides
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_resources: Optional[Dict[str, Any]] = None,
        store: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, str]] = None,
        previous_response_id: Optional[str] = None,
    ) -> Iterator[str]:
        log.infox(
            "responses:ask_stream_start",
            session_id=session_id,
            keep_context=bool(keep_context),
            model=model or self.default_model,
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            has_instructions=instructions is not None,
            tool_count=len(tools or []) if tools is not None else len(self.default_tools or []),
            has_tool_resources=bool(tool_resources),
            store=self.default_store if store is None else bool(store),
            temperature=temperature if temperature is not None else self.default_temperature,
            top_p=top_p if top_p is not None else self.default_top_p,
            max_output_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
            has_response_format=response_format is not None,
            metadata_keys=list((metadata or {}).keys()),
            has_previous_response_id=bool(previous_response_id),
        )

        seq = StepSequence(log, "responses:ask_stream")
        with seq.step("build_request"):
            req = self._build_request(
                user_input=user_input,
                model=model,
                instructions=instructions,
                tools=tools,
                tool_choice=None,
                tool_resources=tool_resources,
                store=store,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                response_format=response_format,
                metadata=metadata,
            )
            prev_id = (
                previous_response_id
                or (self._last_id_by_session.get(session_id) if (keep_context and session_id) else None)
            )
            if prev_id:
                req["previous_response_id"] = prev_id

            log.debugx(
                "responses:ask_stream_request_ready",
                session_id=session_id,
                request_keys=list(req.keys()),
                model=req.get("model"),
                has_previous_response_id=bool(req.get("previous_response_id")),
                previous_response_id=req.get("previous_response_id"),
                has_tools=bool(req.get("tools")),
                tool_count=len(req.get("tools") or []),
            )

        final_resp = None
        delta_count = 0
        total_delta_len = 0
        with seq.step("stream_call"):
            with self.client.responses.stream(**req) as stream:
                log.infox(
                    "responses:stream_opened",
                    session_id=session_id,
                    model=req.get("model"),
                    has_previous_response_id=bool(req.get("previous_response_id")),
                )
                for event in stream:
                    if getattr(event, "type", None) == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            delta_count += 1
                            total_delta_len += len(delta)
                            if delta_count == 1 or delta_count % 25 == 0:
                                log.debugx(
                                    "responses:stream_delta",
                                    session_id=session_id,
                                    delta_count=delta_count,
                                    total_delta_len=total_delta_len,
                                    delta_len=len(delta),
                                )
                            yield delta
                final_resp = stream.get_final_response()

        if session_id and (keep_context or previous_response_id):
            self._last_id_by_session[session_id] = final_resp.id  # type: ignore[attr-defined]
            log.debugx(
                "responses:stream_session_handle_updated",
                session_id=session_id,
                response_id=getattr(final_resp, "id", None),
                known_session_count=len(self._last_id_by_session),
            )

        log.infox(
            "responses:stream_done",
            response_id=getattr(final_resp, "id", None),
            session_id=session_id,
            delta_count=delta_count,
            total_delta_len=total_delta_len,
        )

    def retrieve(self, response_id: str) -> ResponseResult:
        log.infox(
            "responses:retrieve_start",
            response_id=response_id,
        )
        seq = StepSequence(log, "responses:retrieve")
        with seq.step("api_call"), log_context(response_id=response_id):
            resp = self.client.responses.retrieve(response_id=response_id)
        log.debugx(
            "responses:retrieve_api_response",
            requested_response_id=response_id,
            response_id=getattr(resp, "id", None),
            response_type=type(resp).__name__,
            has_output_text=bool(getattr(resp, "output_text", None)),
            output_item_count=len(getattr(resp, "output", []) or []),
        )
        with seq.step("extract_text"):
            text = self._extract_text(resp)
        log.infox("responses:retrieve_done", response_id=resp.id, text_len=len(text or ""))
        return ResponseResult(text=text, response_id=resp.id, raw=resp)

    def fork(
        self,
        from_response_id: str,
        user_input: Union[str, List[Dict[str, str]]],
        **kwargs,
    ) -> ResponseResult:
        log.infox(
            "responses:fork_start",
            from_id=from_response_id,
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            kwarg_keys=list(kwargs.keys()),
        )
        log.debugx("responses:fork", from_id=from_response_id)
        kwargs["previous_response_id"] = from_response_id
        result = self.ask(user_input, **kwargs)
        log.infox(
            "responses:fork_done",
            from_id=from_response_id,
            response_id=result.response_id,
            text_len=len(result.text or ""),
        )
        return result

    def end_session(self, session_id: str) -> None:
        log.infox(
            "responses:end_session",
            session_id=session_id,
            had_session_handle=session_id in self._last_id_by_session,
            had_lock=session_id in self._session_locks,
            known_session_count=len(self._last_id_by_session),
            lock_count=len(self._session_locks),
        )
        self._last_id_by_session.pop(session_id, None)

        # Best-effort cleanup. Only remove if present and not currently locked.
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(session_id, None)
            log.debugx(
                "responses:end_session_lock_removed",
                session_id=session_id,
                lock_count=len(self._session_locks),
            )
        elif lock is not None:
            log.debugx(
                "responses:end_session_lock_kept_because_locked",
                session_id=session_id,
            )

    def get_session_handle(self, session_id: str) -> Optional[str]:
        handle = self._last_id_by_session.get(session_id)
        log.debugx(
            "responses:get_session_handle",
            session_id=session_id,
            found=handle is not None,
            response_id=handle,
        )
        return handle

    # ----------------- Embeddings API -----------------
    def transcribe_audio(
            self,
            file_obj: Any,
            *,
            filename: Optional[str] = None,
            content_type: Optional[str] = None,
            model: Optional[str] = None,
    ) -> str:
        """
        Transcribe an audio file-like object to text.
        `file_obj` should be a binary file handle positioned anywhere (we seek to 0).
        """
        seq = StepSequence(log, "responses:transcribe_audio")
        m = model or self.default_transcription_model
        log.infox(
            "responses:transcribe_start",
            model=m,
            filename=filename,
            content_type=content_type,
            file_obj_type=type(file_obj).__name__,
            has_seek=hasattr(file_obj, "seek"),
        )
        with log_context(model=m, filename=filename, content_type=content_type):
            with seq.step("seek0"):
                try:
                    file_obj.seek(0)
                    log.debugx(
                        "responses:transcribe_seek0_done",
                        filename=filename,
                    )
                except Exception:
                    log.debugx(
                        "responses:transcribe_seek0_skipped_or_failed",
                        filename=filename,
                        file_obj_type=type(file_obj).__name__,
                    )

            with seq.step("api_call"):
                resp = self.client.audio.transcriptions.create(
                    model=m,
                    file=file_obj,
                )

            log.debugx(
                "responses:transcribe_api_response",
                model=m,
                response_type=type(resp).__name__,
                has_text=bool(getattr(resp, "text", None)),
            )

            with seq.step("extract_text"):
                text = (getattr(resp, "text", "") or "").strip()

        log.infox("responses:transcribe_done", model=m, filename=filename, text_len=len(text))
        return text

    def embed(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        normalize: bool = False,
    ) -> List[float]:
        seq = StepSequence(log, "responses:embed")
        log.infox(
            "responses:embed_start",
            model=model or self.default_embedding_model,
            dims=dimensions,
            normalize=bool(normalize),
            text_len=len(text or ""),
        )
        with log_context(model=model or self.default_embedding_model, dims=dimensions, normalize=bool(normalize)):
            with seq.step("api_call"):
                resp = self.client.embeddings.create(
                    model=model or self.default_embedding_model,
                    input=text,
                    dimensions=dimensions,
                )
            log.debugx(
                "responses:embed_api_response",
                model=model or self.default_embedding_model,
                response_type=type(resp).__name__,
                data_count=len(getattr(resp, "data", []) or []),
            )
            with seq.step("extract"):
                vec = list(resp.data[0].embedding)
                raw_dim = len(vec)
                if normalize:
                    vec = self._l2_normalize(vec)
            log.infox("responses:embed_done", out_dim=len(vec), raw_dim=raw_dim, normalized=bool(normalize))
            return vec

    def embed_batch(
        self,
        texts: Sequence[str],
        *,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        normalize: bool = False,
        batch_size: int = 128,
    ) -> List[List[float]]:
        seq = StepSequence(log, "responses:embed_batch")
        embs: List[List[float]] = []
        m = model or self.default_embedding_model
        log.infox(
            "responses:embed_batch_start",
            model=m,
            dims=dimensions,
            normalize=bool(normalize),
            text_count=len(texts or []),
            batch_size=int(batch_size),
            estimated_batches=(len(texts) + batch_size - 1) // batch_size if texts else 0,
        )
        with log_context(model=m, dims=dimensions, normalize=bool(normalize), n=len(texts or []), batch_size=int(batch_size)):
            for i in range(0, len(texts), batch_size):
                chunk = texts[i : i + batch_size]
                log.debugx(
                    "responses:embed_batch_chunk_start",
                    start=i,
                    size=len(chunk),
                    batch_index=(i // batch_size) + 1,
                    model=m,
                )
                with seq.step("api_call_chunk", start=i, size=len(chunk)):
                    resp = self.client.embeddings.create(
                        model=m,
                        input=list(chunk),
                        dimensions=dimensions,
                    )
                log.debugx(
                    "responses:embed_batch_chunk_response",
                    start=i,
                    size=len(chunk),
                    response_type=type(resp).__name__,
                    data_count=len(getattr(resp, "data", []) or []),
                )
                with seq.step("extract_chunk"):
                    for d in resp.data:
                        vec = list(d.embedding)
                        embs.append(self._l2_normalize(vec) if normalize else vec)
                log.debugx(
                    "responses:embed_batch_chunk_done",
                    start=i,
                    size=len(chunk),
                    total_embeddings=len(embs),
                )
            log.infox("responses:embed_batch_done", batches=(len(texts) + batch_size - 1) // batch_size, out=len(embs))
            return embs

    # ----- Embedding utilities (pure Python) -----

    @staticmethod
    def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
        """Cosine similarity between two vectors."""
        log.debugx(
            "responses:cosine_similarity_start",
            len_a=len(a or []),
            len_b=len(b or []),
        )
        num = 0.0
        da = 0.0
        db = 0.0
        for x, y in zip(a, b):
            num += x * y
            da += x * x
            db += y * y
        if da == 0.0 or db == 0.0:
            log.debugx(
                "responses:cosine_similarity_zero_vector",
                da=da,
                db=db,
            )
            return 0.0
        result = num / math.sqrt(da * db)
        log.debugx(
            "responses:cosine_similarity_done",
            result=result,
            da=da,
            db=db,
        )
        return result

    @classmethod
    def cosine_similarities(cls, query: Sequence[float], vectors: Sequence[Sequence[float]]) -> List[float]:
        """Cosine similarity between one query and many vectors."""
        log.debugx(
            "responses:cosine_similarities_start",
            query_dim=len(query or []),
            vector_count=len(vectors or []),
        )
        result = [cls.cosine_similarity(query, v) for v in vectors]
        log.debugx(
            "responses:cosine_similarities_done",
            result_count=len(result),
        )
        return result

    @staticmethod
    def _l2_normalize(v: Sequence[float]) -> List[float]:
        log.debugx(
            "responses:l2_normalize_start",
            dim=len(v or []),
        )
        s = math.sqrt(sum(x * x for x in v))
        result = [x / s for x in v] if s else list(v)
        log.debugx(
            "responses:l2_normalize_done",
            dim=len(result),
            norm=s,
            was_zero=not bool(s),
        )
        return result


    # ----------------- Conversion API ----------------------------------
    def ensure_conversation(self, cache_id: str, conversation_id: Optional[str]) -> str:
        log.infox(
            "responses:ensure_conversation_start",
            cache_id=cache_id,
            has_conversation_id=bool(conversation_id),
            conversation_id=conversation_id,
        )
        if conversation_id:
            log.infox(
                "responses:ensure_conversation_existing",
                cache_id=cache_id,
                conversation_id=conversation_id,
            )
            return conversation_id
        conv = self.client.conversations.create(
            metadata={
                "project": "slimmer-politiek",
                "source": "cache",
                "cache_id": cache_id
            }
        )
        log.infox(
            "responses:ensure_conversation_created",
            cache_id=cache_id,
            conversation_id=conv.id,
        )
        return conv.id

    def seed_conversation(self, conversation_id: str, instruction: str):
        log.infox(
            "responses:seed_conversation_start",
            conversation_id=conversation_id,
            instruction_len=len(instruction or ""),
        )
        self.client.conversations.items.create(
            conversation_id=conversation_id,
            items=[{
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": instruction}],
            }],
        )
        log.infox(
            "responses:seed_conversation_done",
            conversation_id=conversation_id,
            instruction_len=len(instruction or ""),
        )

    def _get_vs_ids_from_conv(self, conversation_id: str) -> list[str]:
        log.infox(
            "responses:get_vs_ids_from_conv_start",
            conversation_id=conversation_id,
        )
        conv = self.client.conversations.retrieve(conversation_id)
        log.debugx(
            "responses:get_vs_ids_from_conv_retrieved",
            conversation_id=conversation_id,
            conversation_type=type(conv).__name__,
            conversation_id_returned=getattr(conv, "id", None),
        )

    def ask_followup(self,
                     conversation_id: str,
                     user_text: str,
                     instructions: str):
        model = self.default_model

        log.infox(
            "responses:ask_followup_start",
            conversation_id=conversation_id,
            model=model,
            user_text_len=len(user_text or ""),
            instructions_len=len(instructions or ""),
        )

        resp = self.client.responses.create(
            model=model,
            conversation=conversation_id,
            instructions=instructions,
            input=[{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
        )

        log.infox(
            "responses:ask_followup_done",
            conversation_id=conversation_id,
            response_id=getattr(resp, "id", None),
            response_type=type(resp).__name__,
        )

        return resp

    # ----------------- Vector stores & files (helpers) -----------------

    def create_vector_store(self, name: str, *, metadata: Optional[Dict[str, str]] = None) -> str:
        """
        Create a new vector store and return its id.
        """
        log.infox(
            "responses:create_vector_store_start",
            name=name,
            metadata_keys=list((metadata or {}).keys()),
        )
        seq = StepSequence(log, "responses:create_vector_store")
        with seq.step("api_call"), log_context(name=name):
            vs = self.client.vector_stores.create(name=name, metadata=metadata or {})
        log.infox("responses:vector_store_created", id=vs.id, name=name)
        return vs.id

    def upload_files(self, paths: List[str]) -> List[str]:
        """
        Upload local files and return a list of file_ids.
        """
        log.infox(
            "responses:upload_files_start",
            count=len(paths or []),
            paths=paths or [],
        )
        seq = StepSequence(log, "responses:upload_files")
        file_ids: List[str] = []
        with log_context(n=len(paths or [])):
            for p in paths:
                log.infox(
                    "responses:upload_file_start",
                    path=p,
                )
                with seq.step("upload_one", path=p):
                    with open(p, "rb") as f:
                        up = self.client.files.create(file=f, purpose="assistants")
                    file_ids.append(up.id)
                log.infox(
                    "responses:upload_file_done",
                    path=p,
                    file_id=up.id,
                    uploaded_count=len(file_ids),
                )
        log.infox("responses:files_uploaded", count=len(file_ids), file_ids=file_ids)
        return file_ids

    def add_files_to_vector_store(self, vector_store_id: str, file_ids: List[str]) -> List[str]:
        """
        Attach uploaded files to a vector store. Returns vector store file ids.
        """
        log.infox(
            "responses:add_files_to_vector_store_start",
            vector_store_id=vector_store_id,
            file_count=len(file_ids or []),
            file_ids=file_ids or [],
        )
        seq = StepSequence(log, "responses:add_files_to_vector_store")
        vs_file_ids: List[str] = []
        with log_context(vector_store_id=vector_store_id, files=len(file_ids or [])):
            for fid in file_ids:
                log.infox(
                    "responses:attach_file_to_vector_store_start",
                    vector_store_id=vector_store_id,
                    file_id=fid,
                )
                with seq.step("attach_one", file_id=fid):
                    vsf = self.client.vector_stores.files.create(
                        vector_store_id=vector_store_id,
                        file_id=fid,
                    )
                    vs_file_ids.append(vsf.id)
                log.infox(
                    "responses:attach_file_to_vector_store_done",
                    vector_store_id=vector_store_id,
                    file_id=fid,
                    vector_store_file_id=vsf.id,
                    attached_count=len(vs_file_ids),
                )
        log.infox("responses:files_attached", vector_store_id=vector_store_id, attached=len(vs_file_ids), vector_store_file_ids=vs_file_ids)
        return vs_file_ids

    def wait_for_vector_store_files(
            self,
            vector_store_id: str,
            *,
            file_ids: Iterable[str] | None = None,
            timeout_s: float = 60.0,
            poll_interval_s: float = 1.5,
            raise_on_failed: bool = True,
    ) -> Tuple[bool, dict]:
        """
        Poll the vector store until all (relevant) files finish indexing.
        Returns (all_completed, status_by_file_id). If raise_on_failed, raises if any failed.
        """
        seq = StepSequence(log, "responses:wait_for_vector_store_files")
        start = time.time()
        wanted = set(file_ids) if file_ids else None
        last = {}

        log.infox(
            "responses:wait_for_vector_store_files_start",
            vector_store_id=vector_store_id,
            wanted_file_count=len(wanted or []),
            wanted_file_ids=list(wanted or []),
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            raise_on_failed=raise_on_failed,
        )

        while True:
            with seq.step("list_files"):
                files = list(self.client.vector_stores.files.list(vector_store_id=vector_store_id))
            status_by_id = {f.id: getattr(f, "status", None) for f in files}
            errs_by_id = {f.id: getattr(f, "last_error", None) for f in files}

            # Filter to the subset we care about, if provided
            ids = wanted if wanted else set(status_by_id.keys())
            subset_status = {i: status_by_id.get(i) for i in ids}
            subset_errs = {i: errs_by_id.get(i) for i in ids}

            # Debug log only when something changes
            if subset_status != last:
                log.debugx("responses:vs_index_status", vector_store_id=vector_store_id, statuses=subset_status)
                last = subset_status

            all_done = all(s in {"completed", "failed", "cancelled"} for s in subset_status.values())
            any_failed = any(s in {"failed", "cancelled"} for s in subset_status.values())

            log.debugx(
                "responses:wait_for_vector_store_files_poll",
                vector_store_id=vector_store_id,
                file_count=len(files),
                subset_count=len(subset_status),
                all_done=all_done,
                any_failed=any_failed,
                elapsed_s=round(time.time() - start, 2),
                statuses=subset_status,
            )

            if all_done:
                if any_failed and raise_on_failed:
                    log.warningx(
                        "responses:wait_for_vector_store_files_failed",
                        vector_store_id=vector_store_id,
                        statuses=subset_status,
                        errors=subset_errs,
                    )
                    raise RuntimeError(f"Vector store indexing failed: {subset_errs}")
                log.infox(
                    "responses:wait_for_vector_store_files_done",
                    vector_store_id=vector_store_id,
                    all_completed=not any_failed,
                    statuses=subset_status,
                    elapsed_s=round(time.time() - start, 2),
                )
                return (not any_failed, {"status": subset_status, "errors": subset_errs})

            if (time.time() - start) > timeout_s:
                log.warningx(
                    "responses:wait_for_vector_store_files_timeout",
                    vector_store_id=vector_store_id,
                    timeout_s=timeout_s,
                    elapsed_s=round(time.time() - start, 2),
                    statuses=subset_status,
                    errors=subset_errs,
                )
                return (False, {"status": subset_status, "errors": subset_errs})

            time.sleep(poll_interval_s)
    # ----------------- Internals -----------------

    def _build_request(
        self,
        *,
        user_input: Union[str, List[Dict[str, str]]],
        model: Optional[str],
        instructions: Optional[str],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        tool_resources: Optional[Dict[str, Any]],
        store: Optional[bool],
        temperature: Optional[float],
        top_p: Optional[float],
        max_output_tokens: Optional[int],
        response_format: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        log.debugx(
            "responses:_build_request_start",
            input_type=type(user_input).__name__,
            input_len=len(str(user_input or "")),
            model=model or self.default_model,
            has_instructions=instructions is not None,
            tools_override=tools is not None,
            tool_count=len(tools or []) if tools is not None else len(self.default_tools or []),
            has_tool_choice=tool_choice is not None,
            has_tool_resources=bool(tool_resources),
            store=self.default_store if store is None else bool(store),
            temperature=temperature if temperature is not None else self.default_temperature,
            top_p=top_p if top_p is not None else self.default_top_p,
            max_output_tokens=max_output_tokens if max_output_tokens is not None else self.default_max_output_tokens,
            has_response_format=response_format is not None,
            metadata_keys=list((metadata or {}).keys()),
        )

        seq = StepSequence(log, "responses:_build_request")
        with seq.step("normalize_input"):
            if isinstance(user_input, str):
                input_payload: Union[str, List[Dict[str, str]]] = user_input
                log.debugx(
                    "responses:_build_request_input_string",
                    input_len=len(input_payload or ""),
                )
            elif isinstance(user_input, list):
                filtered = []
                for m in user_input:
                    role = m.get("role")
                    content = m.get("content")
                    if role in {"user", "assistant"} and content:
                        filtered.append({"role": role, "content": content})
                input_payload = filtered or [{"role": "user", "content": ""}]
                log.debugx(
                    "responses:_build_request_input_messages",
                    input_count=len(user_input),
                    filtered_count=len(filtered),
                    output_count=len(input_payload),
                    roles=[m.get("role") for m in input_payload] if isinstance(input_payload, list) else None,
                )
            else:
                log.warningx(
                    "responses:_build_request_invalid_input",
                    input_type=type(user_input).__name__,
                )
                raise TypeError("user_input must be a str or a list of {'role','content'} dicts")

        with seq.step("assemble_request"):
            req: Dict[str, Any] = {
                "model": model or self.default_model,
                "input": input_payload,
                "instructions": self.default_instructions if instructions is None else instructions,
                "store": self.default_store if store is None else bool(store),
            }

            # tools & tool_resources
            final_tools = self.default_tools if tools is None else tools

            if final_tools:
                req["tools"] = final_tools
                if tool_choice is not None:
                    req["tool_choice"] = tool_choice

            final_tool_resources = dict(self.default_tool_resources)
            if tool_resources:
                for k, v in tool_resources.items():
                    final_tool_resources[k] = v
            if final_tool_resources:
                req["tool_resources"] = final_tool_resources

            # sampling knobs
            if temperature is None:
                temperature = self.default_temperature
            if temperature is not None:
                req["temperature"] = float(temperature)

            if top_p is None:
                top_p = self.default_top_p
            if top_p is not None:
                req["top_p"] = float(top_p)

            if max_output_tokens is None:
                max_output_tokens = self.default_max_output_tokens
            if max_output_tokens is not None:
                req["max_output_tokens"] = int(max_output_tokens)

            if response_format is not None:
                req["response_format"] = response_format

            meta = dict(self.default_metadata)
            if metadata:
                meta.update(metadata)
            if meta:
                req["metadata"] = meta

            log.debugx(
                "responses:_build_request_assembled",
                request_keys=list(req.keys()),
                model=req.get("model"),
                input_type=type(req.get("input")).__name__,
                has_instructions=bool(req.get("instructions")),
                instruction_len=len(req.get("instructions") or ""),
                store=req.get("store"),
                has_tools=bool(req.get("tools")),
                tool_count=len(req.get("tools") or []),
                has_tool_choice=bool(req.get("tool_choice")),
                has_tool_resources=bool(req.get("tool_resources")),
                tool_resource_keys=list((req.get("tool_resources") or {}).keys()),
                temperature=req.get("temperature"),
                top_p=req.get("top_p"),
                max_output_tokens=req.get("max_output_tokens"),
                has_response_format=bool(req.get("response_format")),
                metadata_keys=list((req.get("metadata") or {}).keys()),
            )

        log.debugx(
            "responses:req_built",
            has_tools=bool(req.get("system_tools")),
            has_tool_resources=bool(req.get("tool_resources")),
            temperature=req.get("temperature"),
            top_p=req.get("top_p"),
            max_output_tokens=req.get("max_output_tokens"),
        )
        return req

    @staticmethod
    def _extract_text(resp: Any) -> str:
        """
        Prefer convenience .output_text; fallback to walking structured output.
        """
        log.debugx(
            "responses:extract_text_start",
            response_type=type(resp).__name__,
            response_id=getattr(resp, "id", None),
            has_output_text=bool(getattr(resp, "output_text", None)),
            output_item_count=len(getattr(resp, "output", []) or []),
        )

        txt = getattr(resp, "output_text", None)
        if txt:
            result = txt.strip()
            log.debugx(
                "responses:extract_text_from_output_text",
                response_id=getattr(resp, "id", None),
                text_len=len(result or ""),
            )
            return result

        chunks: List[str] = []
        for item in getattr(resp, "output", []) or []:
            for c in getattr(item, "content", []) or []:
                if hasattr(c, "text") and c.text:
                    chunks.append(c.text)

        result = "\n".join(chunks).strip()
        log.debugx(
            "responses:extract_text_from_structured_output",
            response_id=getattr(resp, "id", None),
            chunk_count=len(chunks),
            text_len=len(result or ""),
        )
        return result