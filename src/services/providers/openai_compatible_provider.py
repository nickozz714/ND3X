"""
services/providers/openai_compatible_provider.py

Adapter for any OpenAI-compatible `/v1` endpoint via a configurable base_url —
covers local runtimes (Ollama, llama.cpp, vLLM, LM Studio) and generic clouds
(Groq, Together, OpenRouter, Azure OpenAI).

Uses the standard Chat Completions + Embeddings APIs (not OpenAI's Responses
API), since that's what compatible servers implement.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger
from services.providers.base import (
    ChatInput,
    ChatProvider,
    ChatResult,
    EmbeddingProvider,
    SpeechProvider,
    TranscriptionProvider,
)

log = get_logger(__name__)


def _compatible_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content or ""
    blocks: List[Dict[str, Any]] = []
    for block in content:
        if block.get("type") in {"text", "input_text"}:
            blocks.append({"type": "text", "text": block.get("text") or ""})
        elif block.get("type") in {"image", "input_image"}:
            blocks.append({"type": "image_url", "image_url": {"url": block.get("image_url") or ""}})
    return blocks


def _to_openai_messages(user_input: ChatInput, instructions: Optional[str]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(user_input, str):
        messages.append({"role": "user", "content": user_input})
    else:
        for m in user_input or []:
            role = (m.get("role") or "user").strip()
            messages.append({"role": role, "content": _compatible_content(m.get("content"))})
    if not any(m["role"] != "system" for m in messages):
        messages.append({"role": "user", "content": ""})
    return messages


def _client_timeout() -> float:
    """Per-request timeout for local/compatible endpoints. Kept bounded (default
    180s) so a slow or unreachable local model surfaces a clear error instead of
    hanging the request for an hour with no status updates."""
    try:
        from component.config import settings
        return float(getattr(settings, "LOCAL_MODEL_TIMEOUT", 180) or 180)
    except Exception:  # noqa: BLE001
        return 180.0


def _compatible_timeout() -> Any:
    """An explicit httpx.Timeout with a bounded READ deadline. A local model that
    produces no bytes within `_client_timeout()`s (e.g. a thinking model stuck
    generating) raises a timeout instead of hanging the turn. A short connect
    timeout surfaces an unreachable endpoint fast."""
    import httpx
    t = _client_timeout()
    return httpx.Timeout(t, connect=10.0)


def _build_async_client(base_url: Optional[str], api_key: Optional[str]) -> Any:
    from openai import AsyncOpenAI  # lazy import
    # Compatible servers (e.g. Ollama) ignore the key but require a non-empty one.
    # max_retries=0: the SDK retries timeouts/conn errors twice by default, which
    # multiplies a stuck local generation into a ~3x wall-clock hang. A timed-out
    # or unreachable local model won't succeed on retry — fail fast instead.
    return AsyncOpenAI(
        base_url=base_url, api_key=api_key or "not-needed",
        timeout=_compatible_timeout(), max_retries=0,
    )


def _build_sync_client(base_url: Optional[str], api_key: Optional[str]) -> Any:
    from openai import OpenAI  # lazy import
    return OpenAI(
        base_url=base_url, api_key=api_key or "not-needed",
        timeout=_compatible_timeout(), max_retries=0,
    )


class OpenAICompatibleChatProvider(ChatProvider):
    provider_type = "openai_compatible"
    supports_structured_output = True
    supports_streaming = True

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None, default_model: str = "", client: Any = None):
        self._default_model = default_model
        self._client = client if client is not None else _build_async_client(base_url, api_key)

    async def chat(
        self,
        user_input: ChatInput,
        *,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        model_id = model or self._default_model
        req: Dict[str, Any] = {
            "model": model_id,
            "messages": _to_openai_messages(user_input, instructions),
        }
        if temperature is not None:
            req["temperature"] = temperature
        if top_p is not None:
            req["top_p"] = top_p
        if max_output_tokens is not None:
            req["max_tokens"] = int(max_output_tokens)
        if response_format:
            # A json_schema response_format constrains generation to the exact
            # schema (Ollama/llama.cpp structured outputs) — this is what makes a
            # local model emit the right field names. Plain json_object otherwise.
            if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
                req["response_format"] = response_format
            else:
                req["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**req)
        except Exception as exc:
            # Some servers/older versions reject a complex json_schema. Degrade to
            # plain JSON mode rather than failing the turn — but ONLY for an actual
            # rejection. A timeout / connection error (e.g. a stuck local model that
            # produced nothing within the read deadline) won't be fixed by dropping
            # the schema, and retrying it just doubles the wall-clock hang, so
            # re-raise those immediately.
            from openai import APIConnectionError, APITimeoutError  # lazy import
            is_schema = isinstance(req.get("response_format"), dict) and req["response_format"].get("type") == "json_schema"
            if is_schema and not isinstance(exc, (APITimeoutError, APIConnectionError)):
                log.warningx("json_schema afgewezen; val terug op json_object", model=model_id)
                req["response_format"] = {"type": "json_object"}
                resp = await self._client.chat.completions.create(**req)
            else:
                raise
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        text = (getattr(getattr(choice, "message", None), "content", None) or "") if choice else ""
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
            "completion_tokens": getattr(usage_obj, "completion_tokens", None),
        }
        try:
            from services.providers.usage_accumulator import add as _usage_add
            _usage_add(input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"],
                       model=model_id, provider_type=self.provider_type)
        except Exception:  # noqa: BLE001
            pass
        return ChatResult(
            text=text,
            response_id=getattr(resp, "id", "") or "",
            raw=resp,
            provider=self.provider_type,
            model=model_id,
            usage=usage,
        )

    async def chat_stream(
        self,
        user_input: ChatInput,
        *,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ):
        """Yield text deltas via chat/completions stream=True. Free-text only (no schema)."""
        model_id = model or self._default_model
        req: Dict[str, Any] = {
            "model": model_id,
            "messages": _to_openai_messages(user_input, instructions),
            "stream": True,
        }
        if max_output_tokens is not None:
            req["max_tokens"] = int(max_output_tokens)
        stream = await self._client.chat.completions.create(**req)
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(getattr(choices[0], "delta", None), "content", None)
            if delta:
                yield delta


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    provider_type = "openai_compatible"

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None, default_model: str = "", client: Any = None):
        self._default_model = default_model
        self._client = client if client is not None else _build_sync_client(base_url, api_key)

    def embed(self, text: str, *, model: Optional[str] = None) -> List[float]:
        resp = self._client.embeddings.create(model=model or self._default_model, input=text)
        return list(resp.data[0].embedding)

    def embed_batch(self, texts: List[str], *, model: Optional[str] = None) -> List[List[float]]:
        resp = self._client.embeddings.create(model=model or self._default_model, input=texts)
        return [list(d.embedding) for d in resp.data]


class OpenAICompatibleTranscriptionProvider(TranscriptionProvider):
    """STT via the OpenAI audio.transcriptions API — covers OpenAI Whisper and
    local whisper servers exposing the same endpoint."""

    provider_type = "openai_compatible"

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None, default_model: Optional[str] = None, client: Any = None):
        # No hardcoded model default: the transcription slot supplies the model
        # (build_transcription_provider passes it), or callers pass it per call.
        self._default_model = default_model
        self._client = client if client is not None else _build_async_client(base_url, api_key)

    async def transcribe(self, audio: bytes, *, model: Optional[str] = None, language: Optional[str] = None, filename: str = "audio.wav") -> str:
        kw: Dict[str, Any] = {"model": model or self._default_model, "file": (filename, audio)}
        if language:
            kw["language"] = language
        resp = await self._client.audio.transcriptions.create(**kw)
        return getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else "") or ""


class OpenAICompatibleSpeechProvider(SpeechProvider):
    """TTS via the OpenAI audio.speech API — covers OpenAI TTS and local
    Piper/compatible servers."""

    provider_type = "openai_compatible"

    def __init__(self, *, base_url: Optional[str], api_key: Optional[str] = None, default_model: Optional[str] = None, default_voice: str = "alloy", client: Any = None):
        # No hardcoded model default: the tts slot supplies the model
        # (build_speech_provider passes it). default_voice is a voice name, not a model.
        self._default_model = default_model
        self._default_voice = default_voice
        self._client = client if client is not None else _build_async_client(base_url, api_key)

    async def synthesize(self, text: str, *, model: Optional[str] = None, voice: Optional[str] = None) -> bytes:
        resp = await self._client.audio.speech.create(
            model=model or self._default_model,
            voice=voice or self._default_voice,
            input=text,
        )
        # SDK returns an object whose .content / .read() yields bytes.
        if hasattr(resp, "content"):
            return resp.content
        if hasattr(resp, "read"):
            return await resp.read() if hasattr(resp.read, "__await__") else resp.read()
        return bytes(resp)
