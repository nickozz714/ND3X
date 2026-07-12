"""
services/providers/base.py

Capability-oriented provider interfaces for the model-agnostic AI platform.

A provider implements one or more capability protocols. The LLMRouter resolves a
model/slot to a concrete provider and calls these methods. Adapters (OpenAI,
Anthropic, OpenAI-compatible, Ollama, ...) implement them.

ChatResult mirrors the legacy ResponseResult shape (text/response_id/raw) so the
router can keep the existing orchestrator surface, and adds normalized
provider/model/usage metadata.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# Content may be text or provider-neutral multimodal blocks used for chat attachments.
ChatInput = Union[str, List[Dict[str, Any]]]


@dataclass
class ChatResult:
    text: str
    response_id: str = ""
    raw: Any = None
    provider: str = ""
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)


class ChatProvider(abc.ABC):
    """A provider that can produce chat/completions, optionally with structured
    output (JSON schema) and streaming."""

    #: provider type id, e.g. "openai", "anthropic", "openai_compatible"
    provider_type: str = "base"
    #: whether this provider honors a JSON-schema response_format
    supports_structured_output: bool = True
    #: whether this provider implements chat_stream (token streaming). Adapters that do
    #: set this True; the router only streams via providers that support it (else it falls
    #: back to a single non-streaming call).
    supports_streaming: bool = False
    #: True for CLI-agent providers (Claude Code; later Codex, ...): the provider
    #: runs its OWN agent loop with its own tools ("agent" execution mode) and
    #: returns a result via an output contract — it cannot enforce structured
    #: output. False = a plain model the orchestrator drives through its own
    #: multi-step logic ("model" execution mode). Subsystems must branch on this
    #: capability, never on the provider_type string.
    is_cli_agent: bool = False

    #: registry of concrete provider classes by provider_type, so capability
    #: lookups by type string (e.g. execution_mode.is_cli_agent_type) don't need
    #: a provider instance. Populated automatically on subclass definition.
    _type_registry: Dict[str, type] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        ptype = getattr(cls, "provider_type", None)
        if ptype and ptype != "base":
            ChatProvider._type_registry[ptype] = cls

    @classmethod
    def class_for_type(cls, provider_type: Optional[str]) -> Optional[type]:
        """The registered ChatProvider subclass for a provider_type string, or
        None when that provider module hasn't been imported/registered."""
        return cls._type_registry.get((provider_type or "").strip())

    @property
    def execution_mode(self) -> str:
        """'agent' when this provider runs its own agent loop (CLI agent),
        'model' when the orchestrator drives it as a plain LLM."""
        return "agent" if self.is_cli_agent else "model"

    @abc.abstractmethod
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
        """Single-shot chat call returning a normalized ChatResult."""
        raise NotImplementedError


class EmbeddingProvider(abc.ABC):
    """A provider that can produce embeddings."""

    provider_type: str = "base"
    #: embedding vector dimension, when known (informational)
    dimension: Optional[int] = None

    @abc.abstractmethod
    def embed(self, text: str, *, model: Optional[str] = None) -> List[float]:
        raise NotImplementedError

    @abc.abstractmethod
    def embed_batch(self, texts: List[str], *, model: Optional[str] = None) -> List[List[float]]:
        raise NotImplementedError


class TranscriptionProvider(abc.ABC):
    """Speech-to-text (recordings). Used by the recordings flow and the cascaded
    voice pipeline."""

    provider_type: str = "base"

    @abc.abstractmethod
    async def transcribe(
        self,
        audio: bytes,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        filename: str = "audio.wav",
    ) -> str:
        raise NotImplementedError


class SpeechProvider(abc.ABC):
    """Text-to-speech. Used by the cascaded voice pipeline."""

    provider_type: str = "base"

    @abc.abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        voice: Optional[str] = None,
    ) -> bytes:
        raise NotImplementedError
