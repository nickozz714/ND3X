# services/voice/voice_webrtc_conversation_service.py
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, Awaitable

from component.config import settings
from component.logging import get_logger
from services.assistants.ask_job_runtime import ask_job_service

log = get_logger("svc.voice_webrtc")


def _ssl_context() -> ssl.SSLContext:
    """
    TLS context for the raw urllib call to OpenAI's realtime endpoint.

    Unlike the OpenAI SDK (httpx) the rest of the app uses, urllib does not pick up
    certifi's CA bundle automatically. On framework/python.org builds the system trust
    store is empty, so verification fails with CERTIFICATE_VERIFY_FAILED and every
    realtime session mint dies at the TLS handshake. Pin certifi's bundle when present
    (it ships transitively with the OpenAI SDK); fall back to the platform default.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

RunAskCallback = Callable[..., Awaitable[Dict[str, Any]]]
StoreUserMessageCallback = Callable[..., Awaitable[None]]
StoreAssistantMessageCallback = Callable[..., Awaitable[None]]
ResponseCallback = Callable[..., Dict[str, Any]]

DEFAULT_REALTIME_INSTRUCTIONS = """
You are the ND3X voice assistant, a concise Jarvis-style operator for Nick's Intelligent Workspace.

Primary behavior:
- Be conversational, calm, direct, and useful.
- For small talk, clarifications, and quick responses, answer directly.
- For any real ND3X work such as searching documents, summarizing files, running workflows,
  using tools, changing data, or checking project state, call start_nd3x_task.
- Never pretend a backend task is done until get_nd3x_task_status or get_nd3x_task_result confirms it.
- When a backend task starts, briefly tell the user you are on it.
- When a backend task completes, summarize the result first. Ask before reading a long answer aloud.
- If a task needs confirmation, read the confirmation question clearly and wait for the user.
- Do not invent document ids, file paths, tool ids, workflow ids, or results.

Voice style:
- Short spoken answers.
- No markdown when speaking unless the user asks for exact text.
- Use natural spoken language.
- If the user interrupts, stop and adapt.

Context rules:
- The active ND3X thread_id is provided by the client/backend.
- For real backend work, always call start_nd3x_task.
- When calling start_nd3x_task, rewrite the user's request as a complete standalone instruction.
- Resolve references from the current voice conversation before calling the tool.
- Do not send vague task questions like "do that", "same one", "continue", or "yes".
- If the referenced object is unclear, ask one concise clarification question before calling start_nd3x_task.

Active task tracking (IMPORTANT — do not forget a delegated task):
- After you call start_nd3x_task, a task is now RUNNING in the background. Remember its
  run_id and that you have an open task — it is your responsibility until it finishes.
- The start_nd3x_task result includes a next_step field while the task is not done. Follow
  it: keep calling get_nd3x_task_status (same thread_id + run_id) until state=completed or
  failed, then read the result. Do not move on as if it were finished.
- Do NOT let small talk make you forget the open task. If the conversation drifts and a task
  is still running, proactively check it and tell the user where it stands.
- You handle ONE task at a time: never start a duplicate task for the same request, and never
  silently drop a started task.
- If you are unsure whether a task is still running, call get_nd3x_task_status rather than
  assuming it is done or forgetting it.

ND3X task behavior:
- For real backend work, call start_nd3x_task.
- start_nd3x_task only starts the task. It does not mean the task is completed.
- To check progress, call get_nd3x_task_status.
- To get the final result, call get_nd3x_task_result.
- Never invent task results.
- Only say a task is completed after get_nd3x_task_status or get_nd3x_task_result says state=completed.
- When get_nd3x_task_result returns voice.spoken_summary, say that summary naturally.
- If browser.append_to_chat is true, the result is also visible in the chat UI.
- If state=awaiting_confirmation or pending_action is present, read the confirmation question aloud and ask the user to answer yes or no.
- Never approve destructive or mutating actions yourself.
- If the user answers yes/no to an active confirmation, call submit_nd3x_confirmation with the same thread_id.
- After submit_nd3x_confirmation, check status/result again before saying it is done.
- If the user asks “are you done?”, “how far are you?”, or similar, call get_nd3x_task_status for the latest active run.
""".strip()


@dataclass
class RealtimeVoiceConfig:
    # realtime_model/task_model are resolved from the realtime/voice routing slots
    # (None when unassigned — the capability is then "not configured", never a
    # hardcoded default). realtime_voice is an output-voice name, not a model.
    realtime_model: Optional[str]
    realtime_voice: str
    task_model: Optional[str]
    max_result_chars: int
    instructions: str


class VoiceWebRTCConversationService:
    """
    Separate WebRTC voice-conversation component.

    This intentionally does not replace:
    - VoiceService: uploaded meeting / Plaud-like transcription jobs.
    - VoiceLiveService: live meeting notes and live markdown updates.

    It only:
    - creates OpenAI Realtime ephemeral client secrets for browser WebRTC sessions;
    - exposes ND3X task bridge methods that the browser calls when the Realtime
      model emits a function/tool call;
    - reuses the existing disk-backed ask_job_service for real orchestrator work.
    """

    def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            config: Optional[RealtimeVoiceConfig] = None,
            run_ask_cb: Optional[RunAskCallback] = None,
            store_user_message_cb: Optional[StoreUserMessageCallback] = None,
            store_assistant_message_cb: Optional[StoreAssistantMessageCallback] = None,
            timeout_response_cb: Optional[ResponseCallback] = None,
            error_response_cb: Optional[ResponseCallback] = None,
    ) -> None:
        # Resolved lazily (see the api_key property) from the registry's OpenAI
        # provider — realtime voice only needs it once a session is actually created.
        self._api_key = api_key
        self.config = config or self._config_from_settings()

        self.run_ask_cb = run_ask_cb
        self.store_user_message_cb = store_user_message_cb
        self.store_assistant_message_cb = store_assistant_message_cb
        self.timeout_response_cb = timeout_response_cb
        self.error_response_cb = error_response_cb

    @property
    def api_key(self):
        if self._api_key:
            return self._api_key
        from services.providers.openai_key import registry_openai_api_key
        return registry_openai_api_key()

    def bind_ask_callbacks(
            self,
            *,
            run_ask_cb: RunAskCallback,
            store_user_message_cb: StoreUserMessageCallback,
            store_assistant_message_cb: StoreAssistantMessageCallback,
            timeout_response_cb: ResponseCallback,
            error_response_cb: ResponseCallback,
    ) -> None:
        self.run_ask_cb = run_ask_cb
        self.store_user_message_cb = store_user_message_cb
        self.store_assistant_message_cb = store_assistant_message_cb
        self.timeout_response_cb = timeout_response_cb
        self.error_response_cb = error_response_cb

    def _require_ask_callbacks(self) -> None:
        missing = []

        if self.run_ask_cb is None:
            missing.append("run_ask_cb")
        if self.store_user_message_cb is None:
            missing.append("store_user_message_cb")
        if self.store_assistant_message_cb is None:
            missing.append("store_assistant_message_cb")
        if self.timeout_response_cb is None:
            missing.append("timeout_response_cb")
        if self.error_response_cb is None:
            missing.append("error_response_cb")

        if missing:
            raise RuntimeError(
                "VoiceWebRTCConversationService ask callbacks are not configured: "
                + ", ".join(missing)
            )

    async def _create_nd3x_ask_job(
            self,
            *,
            question: str,
            payload: Dict[str, Any],
            thread_id: str,
            model: str,
    ) -> Dict[str, Any]:
        self._require_ask_callbacks()

        return await ask_job_service.create_job(
            question=question,
            payload=payload,
            thread_id=thread_id,
            model=model,
            run_ask_cb=self.run_ask_cb,
            store_user_message_cb=self.store_user_message_cb,
            store_assistant_message_cb=self.store_assistant_message_cb,
            timeout_response_cb=self.timeout_response_cb,
            error_response_cb=self.error_response_cb,
        )

    def _state_from_status(self, status: Dict[str, Any]) -> str:
        if not isinstance(status, dict):
            return "unknown"

        state = (
                status.get("state")
                or status.get("status")
                or (status.get("result") or {}).get("state")
                or "unknown"
        )

        return str(state).strip().lower()

    def _extract_ask_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {
                "mode": "unknown",
                "answer": str(result),
                "thread_id": None,
                "pending_action": None,
                "trace": [],
            }

        # Some ask job services wrap the actual response in result.result.
        inner = result.get("result") if isinstance(result.get("result"), dict) else result

        return inner

    def _is_confirmation_result(self, ask_response: Dict[str, Any]) -> bool:
        if not isinstance(ask_response, dict):
            return False

        mode = str(ask_response.get("mode") or "").strip().lower()
        pending_action = ask_response.get("pending_action")

        return bool(pending_action) or mode in {
            "confirm",
            "confirmation",
            "confirm_action",
            "awaiting_confirmation",
        }

    def _build_voice_browser_output(
            self,
            *,
            thread_id: str,
            run_id: str | None,
            state: str,
            ask_response: Dict[str, Any] | None = None,
            status: Dict[str, Any] | None = None,
            error: str | None = None,
            max_chars: int | None = None,
    ) -> Dict[str, Any]:
        ask_response = ask_response or {}
        status = status or {}
        max_chars = max_chars or self.config.max_result_chars

        answer = str(
            ask_response.get("answer")
            or ask_response.get("message")
            or status.get("message")
            or ""
        )

        mode = str(ask_response.get("mode") or "").strip()
        pending_action = ask_response.get("pending_action")
        is_confirmation = self._is_confirmation_result(ask_response)

        if error:
            state = "failed"

        if is_confirmation:
            state = "awaiting_confirmation"

        spoken_summary = answer.replace("\n", " ").strip()
        if len(spoken_summary) > 900:
            spoken_summary = spoken_summary[:900].rsplit(" ", 1)[0] + "..."

        if not spoken_summary:
            if state == "queued":
                spoken_summary = "Ik heb de ND3X taak gestart."
            elif state == "running":
                spoken_summary = "Ik ben nog bezig met de ND3X taak."
            elif state == "completed":
                spoken_summary = "De ND3X taak is afgerond."
            elif state == "awaiting_confirmation":
                spoken_summary = "Ik heb bevestiging nodig voordat ik verder kan."
            elif state == "failed":
                spoken_summary = error or "De ND3X taak is mislukt."
            else:
                spoken_summary = f"De taakstatus is {state}."

        browser_content = answer[:max_chars] if answer else None

        return {
            "ok": not bool(error),
            "thread_id": thread_id,
            "run_id": run_id,
            "state": state,
            "mode": mode,
            "pending_action": pending_action,
            # Strong reminder so the voice model doesn't "forget" it delegated a task: while
            # the run is not terminal, it must keep tracking it and poll until done.
            "next_step": (
                f"PENDING TASK run_id={run_id} is still {state} and NOT done. You MUST keep "
                "this task in mind and follow up: call get_nd3x_task_status with this thread_id "
                "and run_id until state=completed or failed, then get_nd3x_task_result and read "
                "it. Never forget this task, drop it, or start a duplicate for the same request."
            ) if (state in {"queued", "running"} and run_id) else None,
            "voice": {
                "speak": True,
                "spoken_summary": spoken_summary,
                "ask_before_reading_full_answer": bool(answer and len(answer) > 1200),
            },
            "browser": {
                "show_status": True,
                "status_label": self._status_label(state),
                "append_to_chat": bool(state in {"completed", "awaiting_confirmation"} and browser_content),
                "role": "assistant",
                "content": browser_content,
                "show_confirmation": bool(is_confirmation),
                "confirmation_text": answer if is_confirmation else None,
            },
            "raw": {
                "ask_response": ask_response,
                "status": status,
                "error": error,
            },
        }

    def _status_label(self, state: str) -> str:
        return {
            "queued": "Queued",
            "running": "Working in ND3X",
            "completed": "Completed",
            "awaiting_confirmation": "Awaiting confirmation",
            "failed": "Failed",
            "timed_out": "Timed out",
            "rejected": "Rejected",
        }.get(state, state or "Unknown")

    @staticmethod
    def _resolve_task_model() -> Optional[str]:
        """The model for the bridged ND3X task, resolved from the 'voice' slot.

        Returns None when no voice model is assigned; the orchestrator then
        resolves chat from its own routing slots. Never a hardcoded default."""
        from db.database import SessionLocal
        from services.providers.provider_factory import resolve_voice_model
        db = SessionLocal()
        try:
            return resolve_voice_model(db)
        except Exception as exc:  # noqa: BLE001 — never break the voice path
            log.warningx("voice_webrtc:resolve_task_model:failed", error=str(exc))
            return None
        finally:
            db.close()

    @staticmethod
    def _config_from_settings() -> RealtimeVoiceConfig:
        return RealtimeVoiceConfig(
            # realtime_model is resolved per-session from the 'realtime' slot in the
            # /session route (which gates when unassigned); no hardcoded fallback.
            realtime_model=None,
            realtime_voice=(
                getattr(settings, "VOICE_WEBRTC_VOICE", None)
                or os.getenv("VOICE_WEBRTC_VOICE")
                or "marin"
            ),
            # task_model is resolved from the 'voice' slot at task start; None here.
            task_model=None,
            max_result_chars=int(
                getattr(settings, "VOICE_WEBRTC_MAX_RESULT_CHARS", None)
                or os.getenv("VOICE_WEBRTC_MAX_RESULT_CHARS")
                or 6000
            ),
            instructions=(
                getattr(settings, "VOICE_WEBRTC_INSTRUCTIONS", None)
                or os.getenv("VOICE_WEBRTC_INSTRUCTIONS")
                or DEFAULT_REALTIME_INSTRUCTIONS
            ),
        )

    def build_session_config(
        self,
        *,
        thread_id: str,
        payload: Optional[Dict[str, Any]] = None,
        instructions: Optional[str] = None,
        voice: Optional[str] = None,
        realtime_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = payload or {}
        effective_instructions = instructions or payload.get("voice_instructions") or self.config.instructions

        return {
            "session": {
                "type": "realtime",
                "model": realtime_model or payload.get("realtime_model") or self.config.realtime_model,
                "instructions": effective_instructions,
                "audio": {
                    "input": {
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": float(payload.get("vad_threshold", 0.5)),
                            "prefix_padding_ms": int(payload.get("vad_prefix_padding_ms", 300)),
                            "silence_duration_ms": int(payload.get("vad_silence_duration_ms", 550)),
                            "create_response": True,
                            "interrupt_response": True,
                        }
                    },
                    "output": {
                        "voice": voice or payload.get("voice") or self.config.realtime_voice,
                    },
                },
                "tools": self._tools_schema(),
                "tool_choice": "auto",
            }
        }

    async def create_client_secret(
        self,
        *,
        thread_id: str,
        payload: Optional[Dict[str, Any]] = None,
        user_identifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates an ephemeral OpenAI Realtime client secret.
        The browser uses the returned secret to connect to /v1/realtime/calls via WebRTC.
        """
        session_config = self.build_session_config(thread_id=thread_id, payload=payload)
        safety_identifier = self._safety_identifier(user_identifier=user_identifier, thread_id=thread_id)

        log.infox(
            "voice_webrtc:create_client_secret:start",
            thread_id=thread_id,
            realtime_model=session_config["session"].get("model"),
            voice=(session_config["session"].get("audio") or {}).get("output", {}).get("voice"),
            has_safety_identifier=bool(safety_identifier),
        )

        def _call() -> Dict[str, Any]:
            body = json.dumps(session_config).encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/realtime/client_secrets",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "OpenAI-Safety-Identifier": safety_identifier,
                },
            )

            try:
                with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenAI realtime client secret failed: {exc.code} {detail}") from exc

        data = await asyncio.to_thread(_call)

        log.infox(
            "voice_webrtc:create_client_secret:done",
            thread_id=thread_id,
            has_value=bool(data.get("value") or data.get("client_secret", {}).get("value")),
            keys=list(data.keys()) if isinstance(data, dict) else None,
        )

        return {
            **data,
            "thread_id": thread_id,
            "session_config": {
                "model": session_config["session"].get("model"),
                "voice": session_config["session"]["audio"]["output"]["voice"],
                "tool_names": [t.get("name") for t in session_config["session"].get("tools", [])],
            },
            "backend_tools": {
                "start_url": "/main/voice/webrtc/task/start",
                "status_url_template": "/main/voice/webrtc/task/{thread_id}/{run_id}",
                "result_url_template": "/main/voice/webrtc/task/{thread_id}/{run_id}/result",
            },
        }

    async def start_nd3x_task(
            self,
            *,
            question: str,
            thread_id: Optional[str] = None,
            payload: Optional[Dict[str, Any]] = None,
            model: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not (question or "").strip():
            raise ValueError("question is required")

        tid = thread_id or (payload or {}).get("thread_id") or str(uuid.uuid4())

        job_payload = dict(payload or {})
        job_payload["thread_id"] = tid
        job_payload.setdefault("_voice_session", True)
        job_payload.setdefault("_voice_transport", "webrtc")
        job_payload.setdefault("_voice_completion_behavior", "model_polling")

        # Slot-driven: explicit request > payload > 'voice' slot. May be None, in
        # which case the orchestrator resolves chat from its own routing slots.
        use_model = model or job_payload.get("model") or self._resolve_task_model()

        started = await self._create_nd3x_ask_job(
            question=question,
            payload=job_payload,
            thread_id=tid,
            model=use_model,
        )

        run_id = started["run_id"]

        return self._build_voice_browser_output(
            thread_id=tid,
            run_id=run_id,
            state=started.get("state") or "queued",
            status=started,
        )

    def get_nd3x_task_status(self, *, thread_id: str, run_id: str) -> Dict[str, Any]:
        try:
            status = ask_job_service.get_status(thread_id=thread_id, run_id=run_id)
            state = self._state_from_status(status)

            return self._build_voice_browser_output(
                thread_id=thread_id,
                run_id=run_id,
                state=state,
                status=status,
            )

        except Exception as exc:
            return self._build_voice_browser_output(
                thread_id=thread_id,
                run_id=run_id,
                state="failed",
                error=str(exc),
            )

    def get_nd3x_task_result(
            self,
            *,
            thread_id: str,
            run_id: str,
            max_chars: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            result = ask_job_service.get_result(thread_id=thread_id, run_id=run_id)
            ask_response = self._extract_ask_response(result)

            status = {}
            try:
                status = ask_job_service.get_status(thread_id=thread_id, run_id=run_id)
            except Exception:
                status = {}

            state = self._state_from_status(status)

            # If the result itself is a confirmation, override state.
            if self._is_confirmation_result(ask_response):
                state = "awaiting_confirmation"

            if not state or state == "unknown":
                state = "completed"

            return self._build_voice_browser_output(
                thread_id=thread_id,
                run_id=run_id,
                state=state,
                ask_response=ask_response,
                status=status,
                max_chars=max_chars,
            )

        except Exception as exc:
            return self._build_voice_browser_output(
                thread_id=thread_id,
                run_id=run_id,
                state="failed",
                error=str(exc),
            )

    async def submit_nd3x_confirmation(
            self,
            *,
            thread_id: str,
            answer: str,
            payload: Optional[Dict[str, Any]] = None,
            model: Optional[str] = None,
    ) -> Dict[str, Any]:
        value = (answer or "").strip()
        if not value:
            raise ValueError("answer is required")

        normalized = value.lower()

        if normalized in {"yes", "y", "yep", "yeah", "ja", "jep", "doe maar", "akkoord", "bevestig"}:
            question = "ja"
        elif normalized in {"no", "n", "nope", "nee", "annuleer", "cancel", "stop"}:
            question = "nee"
        else:
            question = value

        job_payload = dict(payload or {})
        job_payload["thread_id"] = thread_id
        job_payload.setdefault("_voice_session", True)
        job_payload.setdefault("_voice_transport", "webrtc")
        job_payload.setdefault("_voice_confirmation", True)

        # Slot-driven: explicit request > payload > 'voice' slot. May be None, in
        # which case the orchestrator resolves chat from its own routing slots.
        use_model = model or job_payload.get("model") or self._resolve_task_model()

        started = await self._create_nd3x_ask_job(
            question=question,
            payload=job_payload,
            thread_id=thread_id,
            model=use_model,
        )

        run_id = started["run_id"]

        return self._build_voice_browser_output(
            thread_id=thread_id,
            run_id=run_id,
            state=started.get("state") or "queued",
            status=started,
        )

    @staticmethod
    def _safety_identifier(*, user_identifier: Optional[str], thread_id: str) -> str:
        raw = (user_identifier or thread_id or "anonymous").strip().lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _spoken_status(state: str, status: Dict[str, Any]) -> str:
        if state == "queued":
            return "The task is queued."
        if state == "running":
            return "I am still working on it."
        if state == "awaiting_confirmation":
            return "I need your confirmation before I can continue."
        if state == "completed":
            return "I am done."
        if state in {"failed", "timed_out", "rejected"}:
            msg = status.get("message") or status.get("error") or "The task did not complete."
            return f"The task failed. {str(msg)[:300]}"
        return f"The task state is {state}."

    @staticmethod
    def _compact_result(result: Dict[str, Any], *, max_chars: int) -> Dict[str, Any]:
        if not isinstance(result, dict):
            text = str(result)
            return {
                "mode": "unknown",
                "answer": text[:max_chars],
                "spoken_summary": text[:700],
                "truncated": len(text) > max_chars,
            }

        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        answer = (
            inner.get("answer")
            or inner.get("text")
            or inner.get("summary")
            or inner.get("message")
            or ""
        )

        if not answer:
            answer = json.dumps(inner, ensure_ascii=False, default=str)

        answer = str(answer)
        spoken = answer.strip().replace("\n", " ")
        if len(spoken) > 700:
            spoken = spoken[:700].rsplit(" ", 1)[0] + "..."

        return {
            "mode": inner.get("mode") or result.get("mode"),
            "state": result.get("state") or inner.get("state"),
            "answer": answer[:max_chars],
            "spoken_summary": spoken,
            "pending_action": inner.get("pending_action"),
            "truncated": len(answer) > max_chars,
        }

    @staticmethod
    def _tools_schema() -> list[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "start_nd3x_task",
                "description": (
                    "Start a real ND3X backend task through the orchestrator. Use this for document work, "
                    "searching project data, workflows, tool use, changing data, checking repositories, "
                    "or any task that needs actual ND3X capabilities. Do not use it for casual small talk."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The user's task request rewritten as a clear text instruction for the ND3X orchestrator.",
                        },
                        "thread_id": {
                            "type": "string",
                            "description": "The current ND3X thread id if known.",
                        },
                        "payload": {
                            "type": "object",
                            "description": "Optional ND3X payload/context. Keep small.",
                            "additionalProperties": True,
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional orchestrator model. Usually omit.",
                        },
                    },
                    "required": ["question"],
                },
            },
            {
                "type": "function",
                "name": "get_nd3x_task_status",
                "description": "Check whether a previously started ND3X backend task is queued, running, completed, failed, or awaiting confirmation.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "thread_id": {"type": "string"},
                        "run_id": {"type": "string"},
                    },
                    "required": ["thread_id", "run_id"],
                },
            },
            {
                "type": "function",
                "name": "get_nd3x_task_result",
                "description": "Fetch the compact result for a completed ND3X backend task so it can be summarized or read aloud.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "thread_id": {"type": "string"},
                        "run_id": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000},
                    },
                    "required": ["thread_id", "run_id"],
                },
            },
            {
                "type": "function",
                "name": "submit_nd3x_confirmation",
                "description": (
                    "Submit a yes/no confirmation answer for an active ND3X pending action. "
                    "Use this only after ND3X returned state=awaiting_confirmation or pending_action."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "The active ND3X thread id.",
                        },
                        "answer": {
                            "type": "string",
                            "description": "The user's confirmation answer, usually yes/no or ja/nee.",
                        },
                        "payload": {
                            "type": "object",
                            "description": "Optional small ND3X context.",
                            "additionalProperties": True,
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional orchestrator model. Usually omit.",
                        },
                    },
                    "required": ["thread_id", "answer"],
                },
            }
        ]
