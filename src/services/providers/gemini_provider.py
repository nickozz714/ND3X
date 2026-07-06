from __future__ import annotations

import asyncio
import base64
import re
from typing import Any, Dict, List, Optional

from services.providers.base import ChatInput, ChatProvider, ChatResult, EmbeddingProvider


class GeminiChatProvider(ChatProvider):
    provider_type = "gemini"
    supports_structured_output = True
    supports_streaming = False

    def __init__(self, *, api_key: str, default_model: str, provider_id: int):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._default_model = default_model
        self._provider_id = provider_id

    @staticmethod
    def _contents(user_input: ChatInput) -> tuple[Optional[str], list[Any]]:
        from google.genai import types

        system_parts: list[str] = []
        messages = [{"role": "user", "content": user_input}] if isinstance(user_input, str) else user_input
        contents: list[Any] = []
        for message in messages or []:
            role = message.get("role") or "user"
            content = message.get("content") or ""
            if role == "system":
                system_parts.append(str(content))
                continue
            parts: list[Any] = []
            blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            for block in blocks:
                if block.get("type") in {"text", "input_text"}:
                    parts.append(types.Part.from_text(text=block.get("text") or ""))
                elif block.get("type") in {"image", "input_image"}:
                    match = re.match(r"^data:([^;]+);base64,(.+)$", block.get("image_url") or "")
                    if match:
                        parts.append(types.Part.from_bytes(
                            data=base64.b64decode(match.group(2)), mime_type=match.group(1)
                        ))
            contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))
        return "\n\n".join(system_parts) or None, contents

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
        from google.genai import types
        from services.providers.attachment_context import native_attachment_resources

        embedded_system, contents = self._contents(user_input)
        system = "\n\n".join(value for value in (instructions, embedded_system) if value) or None
        config: Dict[str, Any] = {}
        if system:
            config["system_instruction"] = system
        if temperature is not None:
            config["temperature"] = temperature
        if top_p is not None:
            config["top_p"] = top_p
        if max_output_tokens is not None:
            config["max_output_tokens"] = int(max_output_tokens)
        if response_format:
            config["response_mime_type"] = "application/json"
            if response_format.get("type") == "json_schema":
                config["response_json_schema"] = response_format.get("json_schema", {}).get("schema")
        stores = native_attachment_resources.get().get("gemini_file_search_stores") or {}
        store_name = stores.get(str(self._provider_id))
        if store_name and not response_format:
            config["tools"] = [types.Tool(file_search=types.FileSearch(
                file_search_store_names=[store_name]
            ))]

        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=model or self._default_model,
            contents=contents,
            config=types.GenerateContentConfig(**config),
        )
        usage_obj = getattr(response, "usage_metadata", None)
        usage = {
            "input_tokens": getattr(usage_obj, "prompt_token_count", None),
            "output_tokens": getattr(usage_obj, "candidates_token_count", None),
        }
        try:
            from services.providers.usage_accumulator import add as usage_add
            usage_add(
                input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                model=model or self._default_model, provider_type=self.provider_type,
            )
        except Exception:
            pass
        return ChatResult(
            text=getattr(response, "text", "") or "",
            raw=response,
            provider=self.provider_type,
            model=model or self._default_model,
            usage=usage,
        )


class GeminiEmbeddingProvider(EmbeddingProvider):
    provider_type = "gemini"

    def __init__(self, *, api_key: str, default_model: str):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._default_model = default_model

    def embed(self, text: str, *, model: Optional[str] = None) -> List[float]:
        result = self._client.models.embed_content(model=model or self._default_model, contents=text)
        return list(result.embeddings[0].values)

    def embed_batch(self, texts: List[str], *, model: Optional[str] = None) -> List[List[float]]:
        result = self._client.models.embed_content(model=model or self._default_model, contents=texts)
        return [list(item.values) for item in result.embeddings]
