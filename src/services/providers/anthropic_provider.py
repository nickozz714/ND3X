"""
services/providers/anthropic_provider.py

Anthropic (Claude) chat adapter using the official `anthropic` SDK.

Design notes:
- ND3X embeds the response JSON schema into the prompt and parses the first JSON
  object from the model's text (runtime.base.extract_first_json_object). So this
  adapter returns text and lets the existing parser handle structure — uniform
  across every provider, and avoids per-provider structured-output quirks.
- Adaptive thinking is enabled (Claude decides depth). Thinking blocks are not
  surfaced; only text blocks are concatenated.
- `temperature`/`top_p` are intentionally NOT sent — modern Claude models reject
  them (400).
- A `refusal` stop reason yields empty text + a flag in usage; the orchestrator
  treats empty output as an error.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger
from services.providers.base import ChatInput, ChatProvider, ChatResult

log = get_logger(__name__)

_DEFAULT_MAX_TOKENS = 8192


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content or ""
    blocks: List[Dict[str, Any]] = []
    for block in content:
        if block.get("type") in {"text", "input_text"}:
            blocks.append({"type": "text", "text": block.get("text") or ""})
        elif block.get("type") in {"image", "input_image"}:
            image_url = block.get("image_url") or ""
            match = __import__("re").match(r"^data:([^;]+);base64,(.+)$", image_url)
            if match:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)},
                })
    return blocks


def _split_system_and_messages(user_input: ChatInput, instructions: Optional[str]):
    system_parts: List[str] = []
    if instructions:
        system_parts.append(instructions)
    messages: List[Dict[str, str]] = []
    if isinstance(user_input, str):
        messages.append({"role": "user", "content": user_input})
    else:
        for m in user_input or []:
            role = (m.get("role") or "user").strip()
            content = _anthropic_content(m.get("content"))
            if role == "system":
                system_parts.append(content)
            else:
                messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    if not messages:
        messages.append({"role": "user", "content": ""})
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, messages


def _extract_text(resp: Any) -> str:
    parts: List[str] = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _with_retrieval_documents(
    messages: List[Dict[str, Any]], *, provider_id: int
) -> List[Dict[str, Any]]:
    from services.providers.attachment_context import native_attachment_resources

    documents = native_attachment_resources.get().get("retrieval_documents") or []
    files = (native_attachment_resources.get().get("anthropic_files") or {}).get(str(provider_id)) or []
    if not (documents or files) or not messages:
        return messages
    result = list(messages)
    last = dict(result[-1])
    original = last.get("content")
    content = list(original) if isinstance(original, list) else [{"type": "text", "text": original or ""}]
    document_blocks = [
        {
            "type": "document",
            "source": {"type": "file", "file_id": item["file_id"]},
            "title": item.get("name") or "attachment",
            "citations": {"enabled": True},
            "cache_control": {"type": "ephemeral"},
        }
        for item in files
    ] + [
        {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": item.get("text") or "",
            },
            "title": f"{item.get('name') or 'attachment'} - chunk {int(item.get('chunk') or 0) + 1}",
            "context": "Retrieved from a file attached to the current chat thread.",
            "citations": {"enabled": True},
            "cache_control": {"type": "ephemeral"},
        }
        for item in documents
        if item.get("text")
    ]
    last["content"] = document_blocks + content
    result[-1] = last
    return result


class AnthropicChatProvider(ChatProvider):
    provider_type = "anthropic"
    supports_structured_output = True  # via prompt-embedded schema + text parsing
    supports_streaming = True

    def __init__(self, *, api_key: str, default_model: str = "claude-opus-4-8", client: Any = None,
                 provider_id: int = 0,
                 enable_prompt_caching: bool = False):
        self._default_model = default_model
        self._provider_id = provider_id
        # Anthropic does NOT cache automatically — caching only happens for content blocks
        # explicitly marked with cache_control. When on, we mark the system prompt and the
        # conversation prefix so repeated turns read the stable prefix from cache (~1/10th
        # the input cost) instead of re-billing it in full.
        self._enable_prompt_caching = enable_prompt_caching
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic  # lazy import
            self._client = AsyncAnthropic(api_key=api_key, timeout=60 * 60)

    async def chat(
        self,
        user_input: ChatInput,
        *,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,   # ignored on purpose
        top_p: Optional[float] = None,         # ignored on purpose
        max_output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, messages = _split_system_and_messages(user_input, instructions)
        if not response_format:
            messages = _with_retrieval_documents(messages, provider_id=self._provider_id)
        if response_format and system:
            system = system + (
                "\n\nReturn ONLY a single valid JSON object that matches the required "
                "schema. No prose, no markdown code fences."
            )
        model_id = model or self._default_model

        api_system: Any = system
        api_messages: Any = messages
        if self._enable_prompt_caching:
            # Mark the system prompt and the last message as cache breakpoints: everything up
            # to each breakpoint is written to cache, so the next turn (which appends a new
            # message) reads the whole prior prefix as a cache hit. Max 4 breakpoints; here 2.
            if system:
                api_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if messages and isinstance(messages[-1].get("content"), str):
                last = messages[-1]
                api_messages = messages[:-1] + [{
                    "role": last["role"],
                    "content": [{"type": "text", "text": last.get("content") or "",
                                 "cache_control": {"type": "ephemeral"}}],
                }]

        resp = await self._client.messages.create(
            model=model_id,
            max_tokens=int(max_output_tokens or _DEFAULT_MAX_TOKENS),
            system=api_system,
            messages=api_messages,
            thinking={"type": "adaptive"},
        )
        stop_reason = getattr(resp, "stop_reason", None)
        text = _extract_text(resp)
        if stop_reason == "refusal":
            log.warningx("Anthropic refusal", model=model_id)
            text = ""
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(usage_obj, "input_tokens", None),
            "output_tokens": getattr(usage_obj, "output_tokens", None),
            "stop_reason": stop_reason,
        }
        try:
            from services.providers.usage_accumulator import add as _usage_add
            _usage_add(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
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
        """Yield text deltas. Used for free-text answers (the final-answer writer); not for
        schema-constrained JSON steps."""
        system, messages = _split_system_and_messages(user_input, instructions)
        messages = _with_retrieval_documents(messages, provider_id=self._provider_id)
        model_id = model or self._default_model
        usage_in = usage_out = None
        async with self._client.messages.stream(
            model=model_id,
            max_tokens=int(max_output_tokens or _DEFAULT_MAX_TOKENS),
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
        ) as stream:
            async for delta in stream.text_stream:
                if delta:
                    yield delta
            final = await stream.get_final_message()
            u = getattr(final, "usage", None)
            usage_in = getattr(u, "input_tokens", None)
            usage_out = getattr(u, "output_tokens", None)
        try:
            from services.providers.usage_accumulator import add as _usage_add
            _usage_add(input_tokens=usage_in, output_tokens=usage_out,
                       model=model_id, provider_type=self.provider_type)
        except Exception:  # noqa: BLE001
            pass
