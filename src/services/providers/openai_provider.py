"""
services/providers/openai_provider.py

OpenAI adapter — wraps the existing OpenAIResponsesService so it implements the
capability provider interfaces. This keeps OpenAI behavior identical (delegation)
while making it one provider among many behind the LLMRouter.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.providers.base import ChatInput, ChatProvider, ChatResult, EmbeddingProvider


class OpenAIChatProvider(ChatProvider):
    provider_type = "openai"
    supports_structured_output = True

    def __init__(self, openai_service: Any):
        # openai_service is an OpenAIResponsesService instance.
        self._svc = openai_service

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
        res = await self._svc.ask_async(
            user_input,
            model=model,
            instructions=instructions,
            response_format=response_format,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            metadata=metadata,
            **{k: v for k, v in kwargs.items() if k in {
                "session_id", "keep_context", "tools", "tool_choice",
                "tool_resources", "store", "previous_response_id",
            }},
        )
        return ChatResult(
            text=getattr(res, "text", "") or "",
            response_id=getattr(res, "response_id", "") or "",
            raw=getattr(res, "raw", None),
            provider=self.provider_type,
            model=model or getattr(self._svc, "default_model", ""),
        )


class OpenAIEmbeddingProvider(EmbeddingProvider):
    provider_type = "openai"

    def __init__(self, openai_service: Any):
        self._svc = openai_service

    def embed(self, text: str, *, model: Optional[str] = None) -> List[float]:
        return self._svc.embed(text, model=model) if model else self._svc.embed(text)

    def embed_batch(self, texts: List[str], *, model: Optional[str] = None) -> List[List[float]]:
        return self._svc.embed_batch(texts, model=model) if model else self._svc.embed_batch(texts)
