"""
services/providers/llm_router.py

The LLMRouter is a faithful facade over the legacy OpenAIResponsesService surface
(`ask_orchestration_async`, `ask`, `ask_async`, `ask_stream`, `embed`,
`embed_batch`, `cosine_similarity`). It dispatches chat/embedding calls to an
alternate provider when a resolver returns one for the given model/role; otherwise
it delegates 1:1 to the wrapped OpenAI service.

With no alternate providers configured (the default resolvers return None), the
router is a transparent pass-through — guaranteeing zero behavior change.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, List, Optional, Tuple

from component.logging import get_logger
from services.providers.base import ChatProvider, EmbeddingProvider

log = get_logger(__name__)


async def _bridge_sync_iter(sync_iter) -> AsyncIterator[str]:
    """Consume a SYNC iterator (OpenAI ask_stream) without blocking the event loop: run it
    in a worker thread and hand chunks to the async caller via a queue."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def _pump():
        try:
            for chunk in sync_iter:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as exc:  # noqa: BLE001 — surface as a queue item
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    pump_task = asyncio.create_task(asyncio.to_thread(_pump))
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            if item:
                yield item
    finally:
        await pump_task


# Chat resolver: given the requested model + role, return (provider, model) to
# use — where `model` overrides the requested one (a slot may pin a different
# model) — or None to fall back to OpenAI.
ChatResolution = Tuple[ChatProvider, Optional[str]]
ChatResolver = Callable[[Optional[str], Optional[str]], Optional[ChatResolution]]
# Returns the effective chat MODEL ID for the OpenAI base path (slot-driven),
# given the requested model + role. Never returns settings.LLM_MODEL.
ChatModelResolver = Callable[[Optional[str], Optional[str]], Optional[str]]
EmbeddingResolver = Callable[[Optional[str]], Optional[EmbeddingProvider]]


def _run_coro_sync(coro):
    """Run a coroutine from sync code. If an event loop is already running on
    this thread, execute the coroutine in a separate thread with its own loop so
    we never deadlock; otherwise run it directly."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def _no_chat(_model: Optional[str], _role: Optional[str]) -> Optional[ChatResolution]:
    return None


def _passthrough_chat_model(model: Optional[str], _role: Optional[str]) -> Optional[str]:
    return model


def _no_embedding(_model: Optional[str]) -> Optional[EmbeddingProvider]:
    return None


def _passthrough_embedding_model(model: Optional[str]) -> Optional[str]:
    return model


class LLMRouter:
    def __init__(
        self,
        openai_service: Any,
        *,
        resolve_chat_provider: ChatResolver = _no_chat,
        resolve_chat_model: ChatModelResolver = _passthrough_chat_model,
        resolve_embedding_provider: EmbeddingResolver = _no_embedding,
        resolve_embedding_model: Callable[[Optional[str]], Optional[str]] = _passthrough_embedding_model,
        capabilities: Optional[dict] = None,
        server_side_session: bool = True,
    ):
        self._openai = openai_service
        self._resolve_chat = resolve_chat_provider
        self._resolve_chat_model = resolve_chat_model
        self._resolve_embedding = resolve_embedding_provider
        self._resolve_embedding_model = resolve_embedding_model
        # Capability map from the registry (None = facade/test use, no enforcement).
        self.capabilities = capabilities
        # Whether the OpenAI Responses server-side session may be used (AI Models toggle).
        # When off, even OpenAI runs via the replayed client-side transcript like every
        # other provider — so behaviour is provider-equal.
        self._server_side_session_enabled = server_side_session
        # Mirror attributes some callers read directly.
        self.default_model = getattr(openai_service, "default_model", None)
        self.default_embedding_model = getattr(openai_service, "default_embedding_model", None)

    def _require_chat(self) -> None:
        """Chat is required: with no assigned chat slot (and no chat-picker
        override) there's nothing to fall back to — stop with a clear error."""
        if not self.capabilities:
            return  # facade/test use, or no registry context
        if self.capabilities.get("chat"):
            return
        from services.providers.chat_session import forced_chat_model
        if forced_chat_model.get():
            return
        from services.providers.capability_router import CapabilityNotConfigured
        raise CapabilityNotConfigured("Chat")

    def _require_embeddings(self) -> None:
        if not self.capabilities:
            return
        if self.capabilities.get("embeddings"):
            return
        from services.providers.capability_router import CapabilityNotConfigured
        raise CapabilityNotConfigured("Embeddings")

    @staticmethod
    def _with_openai_file_search(kwargs: dict) -> dict:
        from services.providers.attachment_context import native_attachment_resources

        vector_store_id = native_attachment_resources.get().get("openai_vector_store_id")
        if not vector_store_id:
            return kwargs
        tools = list(kwargs.get("tools") or [])
        if not any(isinstance(tool, dict) and tool.get("type") == "file_search" for tool in tools):
            tools.append({
                "type": "file_search",
                "vector_store_ids": [vector_store_id],
                "max_num_results": 8,
            })
        return {**kwargs, "tools": tools}

    # ── Chat ─────────────────────────────────────────────────────────────────
    async def _dispatch_chat(self, provider: ChatProvider, eff_model: Optional[str], user_input, kwargs, *, role: Optional[str] = None):
        # Tag usage with the stage/role so alternate providers attribute tokens by
        # stage exactly like the OpenAI path (the adapters call usage add() without
        # a role and inherit this). No-op outside a collecting context.
        from services.providers import usage_accumulator as _ua
        tok = _ua.set_role(role)
        try:
            return await provider.chat(
                user_input,
                model=eff_model,
                instructions=kwargs.get("instructions"),
                response_format=kwargs.get("response_format"),
                temperature=kwargs.get("temperature"),
                top_p=kwargs.get("top_p"),
                max_output_tokens=kwargs.get("max_output_tokens"),
                metadata=kwargs.get("metadata"),
            )
        finally:
            _ua.reset_role(tok)

    def supports_server_side_session(self, model: Optional[str] = None, role: Optional[str] = None) -> bool:
        """True when this role/model resolves to the OpenAI Responses base path, which has a
        real server-side session (previous_response_id chaining). Alternate providers
        (Anthropic, local/openai-compatible) run statelessly via provider.chat(), so callers
        must keep sending the full accumulated context in the prompt for them.

        Returns False when the server-side session is disabled by the AI Models toggle —
        then OpenAI also runs via the replayed transcript, so all providers behave the same."""
        if not self._server_side_session_enabled:
            return False
        try:
            return self._resolve_chat(model, role) is None
        except Exception:  # noqa: BLE001 — a probe must never raise into the caller
            return False

    async def ask_orchestration_async(self, user_input, *, role: str, model: Optional[str] = None, **kwargs):
        # The caller's JSON schema (router/planner/cognition) is used to constrain
        # alternate providers; it must not leak into the OpenAI base call.
        json_schema = kwargs.pop("json_schema", None)
        resolved = self._resolve_chat(model, role)
        if resolved is None:
            self._require_chat()
            eff_model = self._resolve_chat_model(model, role)
            return await self._openai.ask_orchestration_async(
                user_input, role=role, model=eff_model, **self._with_openai_file_search(kwargs)
            )
        provider, eff_model = resolved
        # Router + planner/cognition steps must return JSON matching a schema.
        # OpenAI gets this from the instructions; alternate providers (local/
        # compatible) need structured output or they emit wrong field names /
        # <think>/prose. Only the final answer (writer:) is prose.
        if (
            not (role or "").startswith("writer:")
            and not kwargs.get("response_format")
            and getattr(provider, "supports_structured_output", False)
        ):
            if json_schema:
                kwargs = {**kwargs, "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": json_schema, "strict": False},
                }}
            else:
                kwargs = {**kwargs, "response_format": {"type": "json_object"}}
        elif (
            not (role or "").startswith("writer:")
            and json_schema
            and getattr(provider, "is_cli_agent", False)
        ):
            # Fase 4 — decision/generator slots on a CLI-agent provider. A CLI agent
            # can't enforce response_format, so instead of silently dropping the
            # schema we put it IN the prompt and rely on tolerant JSON parsing. This
            # gives memory_decision/auto_decision/wizards a real agent mode (no
            # fallback to another model).
            import json as _json
            _schema_directive = (
                "\n\nReturn ONLY a single JSON object matching this schema — no prose, "
                "no markdown fences:\n" + _json.dumps(json_schema))
            kwargs = {**kwargs, "instructions": (kwargs.get("instructions") or "") + _schema_directive}
        rf = kwargs.get("response_format") or {}
        log.infox("LLMRouter chat naar alternatieve provider", role=role,
                  requested_model=model, effective_model=eff_model or model,
                  provider=provider.provider_type,
                  json_mode=(rf.get("type") if isinstance(rf, dict) else bool(rf)))
        return await self._dispatch_chat(provider, eff_model or model, user_input, kwargs, role=role)

    def chat_provider_type(self, model: Optional[str] = None, role: Optional[str] = None) -> Optional[str]:
        """The provider_type that this role/model resolves to (e.g. 'claude_code',
        'anthropic', 'ollama'), or None when it falls through to the OpenAI base
        path. Lets the pipeline branch on the planner provider (option A: run
        Claude Code as a full agent instead of the ND3X planner loop)."""
        return self.chat_provider_and_model(model, role)[0]

    def chat_provider_and_model(self, model: Optional[str] = None,
                                role: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """(provider_type, effective_model) for this role/model. The effective
        model is what the resolved provider (its routing slot) actually uses —
        for claude_code that's the Claude model assigned on the chat.planner
        slot, NOT any foreign pin the caller passed. Returns (None, None) on the
        OpenAI base path."""
        try:
            resolved = self._resolve_chat(model, role)
        except Exception:  # noqa: BLE001
            return None, None
        if resolved is None:
            return None, None
        provider, eff_model = resolved
        return getattr(provider, "provider_type", None), (eff_model or model)

    def resolves_to_openai(self, model: Optional[str] = None, role: Optional[str] = None) -> bool:
        """True when this role/model uses the OpenAI base path, where the planner JSON comes
        back as free text (json_schema dropped) and can be streamed as text. Alternate
        providers enforce JSON via response_format and are not streamed for the planner."""
        try:
            return self._resolve_chat(model, role) is None
        except Exception:  # noqa: BLE001
            return False

    async def ask_orchestration_stream(
        self,
        user_input,
        *,
        role: str,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream text deltas for a FREE-TEXT call (the final-answer writer). Resolves the
        provider like ask_orchestration_async; OpenAI's sync ask_stream is bridged to async.
        Raises if the resolved alternate provider doesn't support streaming, so the caller
        can fall back to a single non-streaming call."""
        resolved = self._resolve_chat(model, role)
        if resolved is None:
            self._require_chat()
            eff_model = self._resolve_chat_model(model, role)
            openai_kwargs = self._with_openai_file_search(kwargs)
            sync_iter = self._openai.ask_stream(
                user_input, model=eff_model, instructions=instructions,
                max_output_tokens=max_output_tokens, metadata=metadata, **openai_kwargs,
            )
            async for delta in _bridge_sync_iter(sync_iter):
                yield delta
            return
        provider, eff_model = resolved
        if not getattr(provider, "supports_streaming", False):
            raise NotImplementedError(f"Provider {provider.provider_type} does not support streaming")
        from services.providers import usage_accumulator as _ua
        tok = _ua.set_role(role)
        try:
            async for delta in provider.chat_stream(
                user_input, model=eff_model or model, instructions=instructions,
                max_output_tokens=max_output_tokens, metadata=metadata,
            ):
                yield delta
        finally:
            _ua.reset_role(tok)

    async def ask_async(self, user_input, *, model: Optional[str] = None, **kwargs):
        resolved = self._resolve_chat(model, None)
        if resolved is None:
            self._require_chat()
            return await self._openai.ask_async(
                user_input,
                model=self._resolve_chat_model(model, None),
                **self._with_openai_file_search(kwargs),
            )
        provider, eff_model = resolved
        return await self._dispatch_chat(provider, eff_model or model, user_input, kwargs, role=kwargs.get("role"))

    def ask(self, user_input, *, model: Optional[str] = None, role: Optional[str] = None, **kwargs):
        json_schema = kwargs.pop("json_schema", None)
        resolved = self._resolve_chat(model, role)
        if resolved is None:
            self._require_chat()
            return self._openai.ask(user_input, model=self._resolve_chat_model(model, role), **kwargs)
        provider, eff_model = resolved
        if (
            not (role or "").startswith("writer:")
            and not kwargs.get("response_format")
            and getattr(provider, "supports_structured_output", False)
        ):
            if json_schema:
                kwargs = {**kwargs, "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": json_schema, "strict": False},
                }}
            else:
                kwargs = {**kwargs, "response_format": {"type": "json_object"}}
        # Alternate providers are async; bridge to a coroutine without blocking a
        # running event loop (run it in a worker thread that owns its own loop).
        return _run_coro_sync(self._dispatch_chat(provider, eff_model or model, user_input, kwargs, role=role))

    def ask_stream(self, *args, **kwargs):
        # Streaming stays on OpenAI until a provider implements it (Phase 4 voice).
        return self._openai.ask_stream(*args, **kwargs)

    # ── Embeddings ───────────────────────────────────────────────────────────
    def embed(self, text: str, *, model: Optional[str] = None, **kwargs) -> List[float]:
        provider = self._resolve_embedding(model)
        if provider is None:
            self._require_embeddings()
            eff_model = self._resolve_embedding_model(model)
            return self._openai.embed(text, model=eff_model, **kwargs) if (eff_model or kwargs) else self._openai.embed(text)
        return provider.embed(text, model=model)

    def embed_batch(self, texts: List[str], *, model: Optional[str] = None, **kwargs) -> List[List[float]]:
        provider = self._resolve_embedding(model)
        if provider is None:
            self._require_embeddings()
            eff_model = self._resolve_embedding_model(model)
            return self._openai.embed_batch(texts, model=eff_model, **kwargs) if (eff_model or kwargs) else self._openai.embed_batch(texts)
        return provider.embed_batch(texts, model=model)

    def cosine_similarity(self, *args, **kwargs):
        return self._openai.cosine_similarity(*args, **kwargs)

    def embedding_identity(self) -> str:
        """Stable-enough identity used to avoid comparing vectors from different spaces."""
        provider = self._resolve_embedding(None)
        if provider is not None:
            model = getattr(provider, "_default_model", "") or "default"
            return f"{getattr(provider, 'provider_type', 'embedding')}:{model}"
        return f"openai:{self._resolve_embedding_model(None) or 'default'}"

    # Pass through any other attribute access to the underlying OpenAI service so
    # the router stays a safe drop-in (voice/transcription/etc. unaffected).
    def __getattr__(self, name: str) -> Any:
        return getattr(self._openai, name)
