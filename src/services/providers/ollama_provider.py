"""
services/providers/ollama_provider.py

Native Ollama chat adapter on /api/chat.

Why native instead of the OpenAI-compatible /v1 endpoint: /v1 cannot set the
context window, so Ollama runs every request at its server default (4096) and
SILENTLY TRUNCATES longer prompts — the model then never sees the system
instructions or the tool manifest (observed: "truncating input prompt"
limit=4095 prompt=8510 in the Ollama server log). The native API accepts
`options.num_ctx` per request, so each call gets a context window sized for
the model: min(model's configured context_window, settings.OLLAMA_NUM_CTX).

Also native-only wins used here:
- `format` accepts a full JSON schema (structured outputs) or "json".
- The response reports prompt_eval_count/eval_count for real usage accounting,
  and lets us detect likely truncation instead of failing silently.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from component.logging import get_logger
from services.providers.base import ChatInput, ChatProvider, ChatResult

log = get_logger(__name__)

# Never go below Ollama's own default; a tiny window is how prompts get eaten.
MIN_NUM_CTX = 4096


def _num_ctx_setting() -> int:
    try:
        from component.config import settings
        return int(getattr(settings, "OLLAMA_NUM_CTX", 16384) or 16384)
    except Exception:  # noqa: BLE001
        return 16384


def _client_timeout() -> float:
    try:
        from component.config import settings
        return float(getattr(settings, "LOCAL_MODEL_TIMEOUT", 180) or 180)
    except Exception:  # noqa: BLE001
        return 180.0


def _native_base(base_url: Optional[str]) -> str:
    """Providers registered for Ollama usually store the OpenAI-compat base
    (http://host:11434/v1); the native API lives at the root."""
    base = (base_url or "http://localhost:11434").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _to_ollama_messages(user_input: ChatInput, instructions: Optional[str]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(user_input, str):
        messages.append({"role": "user", "content": user_input})
    else:
        for m in user_input or []:
            role = (m.get("role") or "user").strip()
            content = m.get("content")
            if isinstance(content, list):
                # Provider-neutral blocks → native shape: text concatenated,
                # data-URL images moved to the `images` field (base64 payload).
                texts: List[str] = []
                images: List[str] = []
                for block in content:
                    btype = block.get("type")
                    if btype in {"text", "input_text"}:
                        texts.append(block.get("text") or "")
                    elif btype in {"image", "input_image"}:
                        url = block.get("image_url") or ""
                        if isinstance(url, str) and url.startswith("data:") and "," in url:
                            images.append(url.split(",", 1)[1])
                msg: Dict[str, Any] = {"role": role, "content": "\n".join(t for t in texts if t)}
                if images:
                    msg["images"] = images
                messages.append(msg)
            else:
                messages.append({"role": role, "content": content or ""})
    if not any(m["role"] != "system" for m in messages):
        messages.append({"role": "user", "content": ""})
    return messages


def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Cheap char-based estimate (~3.5 chars/token) to size num_ctx and to warn
    when a prompt cannot fit the window at all."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
    return int(chars / 3.5) + 64


class OllamaChatProvider(ChatProvider):
    provider_type = "ollama"
    supports_structured_output = True
    supports_streaming = True

    def __init__(
        self,
        *,
        base_url: Optional[str],
        default_model: str = "",
        model_ctx: Optional[Dict[str, int]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base = _native_base(base_url)
        self._default_model = default_model
        # model_id -> configured context_window (provider_models.context_window)
        self._model_ctx = dict(model_ctx or {})
        self._client = client

    def _resolve_num_ctx(self, model_id: str, messages: List[Dict[str, Any]]) -> int:
        cap = max(_num_ctx_setting(), MIN_NUM_CTX)
        configured = int(self._model_ctx.get(model_id) or 0)
        num_ctx = min(configured, cap) if configured else cap
        num_ctx = max(num_ctx, MIN_NUM_CTX)
        est = _estimate_tokens(messages)
        if est > num_ctx:
            log.warningx(
                "Ollama prompt past mogelijk niet in num_ctx — verhoog OLLAMA_NUM_CTX of "
                "het context window van het model, of gebruik light mode",
                model=model_id, estimated_tokens=est, num_ctx=num_ctx,
            )
        return num_ctx

    async def _post(self, path: str, body: Dict[str, Any], *, stream: bool = False):
        timeout = httpx.Timeout(_client_timeout(), connect=10.0)
        if self._client is not None:
            return await self._client.request("POST", f"{self._base}{path}", json=body)
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.request("POST", f"{self._base}{path}", json=body)

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
        messages = _to_ollama_messages(user_input, instructions)
        num_ctx = self._resolve_num_ctx(model_id, messages)
        options: Dict[str, Any] = {"num_ctx": num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        if top_p is not None:
            options["top_p"] = top_p
        if max_output_tokens is not None:
            options["num_predict"] = int(max_output_tokens)
        body: Dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if response_format:
            fmt = self._to_format(response_format)
            if fmt is not None:
                body["format"] = fmt

        resp = await self._post("/api/chat", body)
        if resp.status_code >= 400:
            err = self._body_error(resp)
            raise RuntimeError(f"Ollama chat failed for '{model_id}': {err}")
        data = resp.json()
        text = ((data.get("message") or {}).get("content")) or ""
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        # A prompt that exactly fills the window is the truncation signature —
        # surface it loudly; a truncated planner prompt is unusable, not just slow.
        if isinstance(prompt_tokens, int) and prompt_tokens >= num_ctx - 16:
            log.warningx(
                "Ollama prompt vult het volledige num_ctx — waarschijnlijk afgekapt",
                model=model_id, prompt_tokens=prompt_tokens, num_ctx=num_ctx,
            )
        try:
            from services.providers.usage_accumulator import add as _usage_add
            _usage_add(input_tokens=prompt_tokens, output_tokens=completion_tokens,
                       model=model_id, provider_type=self.provider_type)
        except Exception:  # noqa: BLE001
            pass
        return ChatResult(
            text=text,
            response_id=str(data.get("created_at") or ""),
            raw=data,
            provider=self.provider_type,
            model=model_id,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
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
        """Yield text deltas from the native NDJSON stream. Free-text only."""
        model_id = model or self._default_model
        messages = _to_ollama_messages(user_input, instructions)
        num_ctx = self._resolve_num_ctx(model_id, messages)
        body: Dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "options": {"num_ctx": num_ctx},
        }
        if max_output_tokens is not None:
            body["options"]["num_predict"] = int(max_output_tokens)
        timeout = httpx.Timeout(_client_timeout(), connect=10.0)
        client = self._client
        own = client is None
        if own:
            client = httpx.AsyncClient(timeout=timeout)
        try:
            async with client.stream("POST", f"{self._base}/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise RuntimeError(
                        f"Ollama chat stream failed for '{model_id}': {self._body_error(resp)}"
                    )
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001 — skip a garbled line
                        continue
                    if obj.get("error"):
                        raise RuntimeError(f"Ollama chat stream error: {obj['error']}")
                    delta = ((obj.get("message") or {}).get("content")) or ""
                    if delta:
                        yield delta
        finally:
            if own:
                await client.aclose()

    @staticmethod
    def _to_format(response_format: Dict[str, Any]) -> Any:
        """OpenAI response_format → native `format`: a json_schema becomes the raw
        schema object (Ollama structured outputs), anything else JSON mode."""
        if not isinstance(response_format, dict):
            return None
        if response_format.get("type") == "json_schema":
            schema = ((response_format.get("json_schema") or {}).get("schema")) or None
            return schema if isinstance(schema, dict) else "json"
        return "json"

    @staticmethod
    def _body_error(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                return str(data["error"])
        except Exception:  # noqa: BLE001
            pass
        return (resp.text or "").strip() or f"HTTP {resp.status_code}"
