"""
services/providers/provider_factory.py

Builds concrete provider adapters from the DB registry and assembles an
LLMRouter wired to resolve calls against it.

Resolution policy (Phase 1):
- Chat: by explicit model string. An assistant configured with model
  "claude-opus-4-8" routes to the Anthropic provider that registers that model.
  OpenAI/gpt models fall through to the wrapped OpenAI service (identical behavior).
- Embeddings: by the "embeddings" capability slot (one embedding space), with the
  slot's model baked into the adapter as its default. An explicitly-registered
  embedding model also routes directly.

With an empty registry, both resolvers return None → transparent OpenAI pass-through.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.provider import Provider
from services.providers.anthropic_provider import AnthropicChatProvider
from services.providers.base import ChatProvider, EmbeddingProvider
from services.providers.llm_router import LLMRouter
from services.providers.openai_compatible_provider import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleSpeechProvider,
    OpenAICompatibleTranscriptionProvider,
)
from services.providers.openai_provider import OpenAIChatProvider, OpenAIEmbeddingProvider
from services.providers.gemini_provider import GeminiChatProvider, GeminiEmbeddingProvider
from services.providers.registry_service import ProviderRegistryService

log = get_logger(__name__)


def role_to_slot(role: Optional[str]) -> str:
    """Map an orchestration role string to a chat capability slot.

    Roles look like: 'router:{turn}', 'writer:{name}:{turn}' (final answer),
    'assistant:{name}' / 'workflow_planner:...' / 'planner_continue:...' (planner),
    'cognition:{name}' (memory/belief extraction), 'memory_decision:{kind}'
    (whether to retrieve memories). Each maps to its own capability slot so a
    distinct model can be assigned per internal step.
    """
    r = (role or "")
    if r.startswith("router:"):
        return "chat.router"
    if r.startswith("writer:"):
        return "chat.final_answer"
    if r.startswith("memory_decision:"):
        return "chat.memory_decision"
    if r.startswith("cognition:"):
        return "chat.cognition"
    return "chat.planner"


def _build_chat_provider(p: Provider, api_key: Optional[str], default_model: str, openai_service: Any,
                         *, enable_prompt_caching: bool = False) -> Optional[ChatProvider]:
    t = (p.provider_type or "").strip()
    if t == "anthropic":
        if not api_key:
            log.warningx("Anthropic provider zonder API key", provider_id=p.id)
            return None
        return AnthropicChatProvider(api_key=api_key, default_model=default_model,
                                     provider_id=p.id,
                                     enable_prompt_caching=enable_prompt_caching)
    if t == "ollama":
        # Native /api/chat so each call gets a real context window (options.num_ctx);
        # the OpenAI-compat /v1 endpoint runs at Ollama's 4096 default and silently
        # truncates longer prompts.
        from services.providers.ollama_provider import OllamaChatProvider
        model_ctx = {
            (m.model_id or ""): int(m.context_window)
            for m in (p.models or [])
            if m.capability == "chat" and m.context_window
        }
        return OllamaChatProvider(base_url=p.base_url, default_model=default_model, model_ctx=model_ctx)
    if t == "openai_compatible":
        return OpenAICompatibleChatProvider(base_url=p.base_url, api_key=api_key, default_model=default_model)
    if t == "openai":
        return OpenAIChatProvider(openai_service)
    if t == "gemini":
        if not api_key:
            log.warningx("Gemini provider zonder API key", provider_id=p.id)
            return None
        return GeminiChatProvider(api_key=api_key, default_model=default_model, provider_id=p.id)
    if t == "claude_code":
        # Headless Claude Code CLI on the local machine. The stored "API key" is
        # the `claude setup-token` OAuth token (subscription auth); without it
        # the CLI falls back to the host login (~/.claude). Extra knobs come
        # from config_json — see claude_code_provider.py for the key list.
        import json as _json
        from services.providers.claude_code_provider import ClaudeCodeChatProvider
        cfg: Dict[str, Any] = {}
        try:
            cfg = _json.loads(p.config_json or "{}") or {}
        except Exception:  # noqa: BLE001
            log.warningx("Claude Code provider config_json niet parsebaar — defaults gebruikt",
                         provider_id=p.id)
        return ClaudeCodeChatProvider(
            default_model=default_model,
            oauth_token=api_key,
            cli_path=str(cfg.get("cli_path") or "claude"),
            agentic=bool(cfg.get("agentic")),
            max_turns=cfg.get("max_turns"),
            timeout=cfg.get("timeout"),
            workdir=cfg.get("workdir"),
            allowed_tools=cfg.get("allowed_tools"),
            extra_args=cfg.get("extra_args"),
        )
    log.warningx("Onbekend chat provider type", provider_type=t)
    return None


def _build_embedding_provider(p: Provider, api_key: Optional[str], default_model: str, openai_service: Any) -> Optional[EmbeddingProvider]:
    t = (p.provider_type or "").strip()
    if t in ("openai_compatible", "ollama", "voyage"):
        return OpenAICompatibleEmbeddingProvider(base_url=p.base_url, api_key=api_key, default_model=default_model)
    if t == "gemini" and api_key:
        return GeminiEmbeddingProvider(api_key=api_key, default_model=default_model)
    if t == "openai":
        return OpenAIEmbeddingProvider(openai_service)
    # anthropic has no embeddings
    return None


def build_llm_router(openai_service: Any, db: Session) -> LLMRouter:
    reg = ProviderRegistryService(db)

    # Runtime behaviour toggles (AI Models UI). Read at build time (build is per request),
    # so flipping a toggle takes effect on the next turn. Never break the orchestrator.
    try:
        from services.llm_runtime_settings import (
            prompt_caching_enabled,
            openai_server_side_session_enabled,
        )
        caching_on = prompt_caching_enabled(db)
        server_side_session = openai_server_side_session_enabled(db)
    except Exception as exc:  # noqa: BLE001
        log.warningx("LLM runtime settings laden mislukt; defaults", error=str(exc))
        caching_on, server_side_session = True, False

    # Preload enabled chat models: model_id -> Provider (last registration wins).
    chat_models: Dict[str, Provider] = {}
    try:
        for m in reg.list_models(capability="chat"):
            if not m.enabled:
                continue
            p = reg.get_provider(m.provider_id)
            if p and p.enabled:
                chat_models[m.model_id] = p
    except Exception as exc:  # noqa: BLE001 — registry must never break the orchestrator
        log.warningx("Chat model registry laden mislukt; OpenAI fallback", error=str(exc))

    chat_cache: Dict[int, Optional[ChatProvider]] = {}

    def _chat_provider_for(p: Provider, default_model: str) -> Optional[ChatProvider]:
        if p.id in chat_cache:
            return chat_cache[p.id]
        prov = _build_chat_provider(p, reg.get_api_key(p.id), default_model, openai_service,
                                    enable_prompt_caching=caching_on)
        chat_cache[p.id] = prov
        return prov

    slot_cache: Dict[str, Optional[tuple]] = {}

    def _slot_provider(slot: str) -> Optional[tuple]:
        """(provider, model) for a routing slot, or None for OpenAI/unassigned."""
        if slot in slot_cache:
            return slot_cache[slot]
        result = None
        try:
            resolved = reg.resolve_slot(slot)
            if resolved and (resolved.provider_type or "") != "openai":
                p = reg.get_provider(resolved.provider_id)
                if p:
                    prov = _chat_provider_for(p, resolved.model_id)
                    if prov is not None:
                        result = (prov, resolved.model_id)
        except Exception as exc:  # noqa: BLE001 — never break the orchestrator
            log.warningx("Chat slot resolutie mislukt", slot=slot, error=str(exc))
        slot_cache[slot] = result
        return result

    # Fallback among ASSIGNED chat slots only (slots are authoritative — no
    # arbitrary registered model is invented). chat.planner is the primary; an
    # unassigned router/final_answer role borrows whichever chat slot is set.
    default_holder: Dict[str, Optional[tuple]] = {}

    def _any_chat_slot() -> Optional[tuple]:
        if "v" in default_holder:
            return default_holder["v"]
        result = None
        for slot in ("chat.planner", "chat.cognition"):
            r = _slot_provider(slot)
            if r is not None:
                result = r
                break
        default_holder["v"] = result
        return result

    def resolve_chat(model: Optional[str], role: Optional[str]):
        # 0) A model explicitly picked in the chat UI (forced) is authoritative for
        #    this request — it overrides the workbench routing slot.
        from services.providers.chat_session import forced_chat_model
        forced = forced_chat_model.get()
        if forced:
            p = chat_models.get(forced)
            if p is not None and (p.provider_type or "") != "openai":
                prov = _chat_provider_for(p, forced)
                if prov is not None:
                    return (prov, forced)
            return None  # forced registered-OpenAI/unknown model -> OpenAI base
        # 1) The Routing selection (capability slot for this role) takes precedence.
        if role:
            slotted = _slot_provider(role_to_slot(role))
            if slotted is not None:
                return slotted
        # 2) An explicitly-requested *registered* model (e.g. an assistant pinned
        #    to "claude-opus-4-8", or a registered OpenAI model -> OpenAI base).
        if model and model in chat_models:
            p = chat_models[model]
            if (p.provider_type or "") != "openai":
                prov = _chat_provider_for(p, model)
                if prov is not None:
                    return (prov, model)
            return None  # registered OpenAI model -> OpenAI base
        # 3) Unassigned role borrows an assigned chat slot (slots are
        #    authoritative). If no chat slot is assigned at all this returns None
        #    and the LLMRouter enforces the required-capability error.
        return _any_chat_slot()

    # Effective chat MODEL ID for the OpenAI base path (when resolve_chat returns
    # None because the slot is OpenAI-backed or unassigned). Without this, the
    # OpenAI base call used whatever model string the caller passed — i.e.
    # settings.LLM_MODEL — and an OpenAI-backed chat slot was silently ignored.
    # Precedence mirrors resolve_chat: forced UI pick > role's slot > explicit
    # registered model > any assigned chat slot. Never settings.LLM_MODEL.
    slot_model_cache: Dict[str, Optional[str]] = {}

    def _slot_model_id(slot: str) -> Optional[str]:
        if slot in slot_model_cache:
            return slot_model_cache[slot]
        mid = None
        try:
            resolved = reg.resolve_slot(slot)
            if resolved:
                mid = resolved.model_id
        except Exception as exc:  # noqa: BLE001 — never break the orchestrator
            log.warningx("Chat slot model resolutie mislukt", slot=slot, error=str(exc))
        slot_model_cache[slot] = mid
        return mid

    def resolve_chat_model(model: Optional[str], role: Optional[str]) -> Optional[str]:
        from services.providers.chat_session import forced_chat_model
        forced = forced_chat_model.get()
        if forced:
            return forced
        if role:
            mid = _slot_model_id(role_to_slot(role))
            if mid:
                return mid
        if model:
            return model
        for slot in ("chat.planner", "chat.cognition"):
            mid = _slot_model_id(slot)
            if mid:
                return mid
        return model

    # Embeddings: resolve via the "embeddings" slot (model baked into adapter).
    emb_holder: Dict[str, Optional[EmbeddingProvider]] = {}

    def resolve_embedding(model: Optional[str]) -> Optional[EmbeddingProvider]:
        if "default" in emb_holder:
            return emb_holder["default"]
        prov: Optional[EmbeddingProvider] = None
        try:
            resolved = reg.resolve_slot("embeddings")
            if resolved and (resolved.provider_type or "") != "openai":
                p = reg.get_provider(resolved.provider_id)
                if p:
                    prov = _build_embedding_provider(p, reg.get_api_key(p.id), resolved.model_id, openai_service)
        except Exception as exc:  # noqa: BLE001
            log.warningx("Embedding slot resolutie mislukt; OpenAI fallback", error=str(exc))
        emb_holder["default"] = prov
        return prov

    # Effective embedding MODEL ID for the OpenAI base path (when resolve_embedding
    # returns None because the embeddings slot is OpenAI-backed or unassigned).
    # The embeddings slot defines THE embedding space, so it is authoritative over
    # any caller-passed model. Never settings.EMBEDDING_MODEL.
    def resolve_embedding_model(model: Optional[str]) -> Optional[str]:
        try:
            resolved = reg.resolve_slot("embeddings")
            if resolved and resolved.model_id:
                return resolved.model_id
        except Exception as exc:  # noqa: BLE001 — never break the orchestrator
            log.warningx("Embedding slot model resolutie mislukt", error=str(exc))
        return model

    from services.providers.capability_router import compute_capabilities
    return LLMRouter(
        openai_service,
        resolve_chat_provider=resolve_chat,
        resolve_chat_model=resolve_chat_model,
        resolve_embedding_provider=resolve_embedding,
        resolve_embedding_model=resolve_embedding_model,
        capabilities=compute_capabilities(db),
        server_side_session=server_side_session,
    )


# Recordings / voice resolution (Phase 3/4). These return None when no slot is
# assigned, so callers fall back to the existing OpenAI voice path.
def build_transcription_provider(db: Session):
    reg = ProviderRegistryService(db)
    resolved = reg.resolve_slot("transcription")
    if not resolved:
        return None
    p = reg.get_provider(resolved.provider_id)
    if not p or (p.provider_type or "") not in ("openai_compatible", "ollama"):
        return None
    return OpenAICompatibleTranscriptionProvider(
        base_url=p.base_url, api_key=reg.get_api_key(p.id), default_model=resolved.model_id,
    )


def resolve_embedding_provider(db: Session, openai_service: Any):
    """Embedding provider for the 'embeddings' slot, or None to use the base
    (OpenAI) embeddings. Safe-by-default: only routes when a slot is assigned."""
    reg = ProviderRegistryService(db)
    try:
        resolved = reg.resolve_slot("embeddings")
    except Exception as exc:  # noqa: BLE001
        log.warningx("Embedding slot resolutie mislukt", error=str(exc))
        return None
    if not resolved or (resolved.provider_type or "") == "openai":
        return None
    p = reg.get_provider(resolved.provider_id)
    if not p:
        return None
    return _build_embedding_provider(p, reg.get_api_key(p.id), resolved.model_id, openai_service)


def resolve_default_chat_provider(db: Session, openai_service: Any):
    """A chat provider for the cascaded voice pipeline: the first enabled
    non-OpenAI chat model's provider, else the OpenAI adapter."""
    reg = ProviderRegistryService(db)
    try:
        for m in reg.list_models(capability="chat"):
            if not m.enabled:
                continue
            p = reg.get_provider(m.provider_id)
            if p and p.enabled and (p.provider_type or "") != "openai":
                prov = _build_chat_provider(p, reg.get_api_key(p.id), m.model_id, openai_service)
                if prov is not None:
                    return prov
    except Exception as exc:  # noqa: BLE001
        log.warningx("Default chat provider resolutie mislukt", error=str(exc))
    return OpenAIChatProvider(openai_service)


def build_speech_provider(db: Session):
    reg = ProviderRegistryService(db)
    for m in reg.list_models(capability="tts"):
        if not m.enabled:
            continue
        p = reg.get_provider(m.provider_id)
        if p and p.enabled and (p.provider_type or "") in ("openai_compatible", "ollama"):
            return OpenAICompatibleSpeechProvider(
                base_url=p.base_url, api_key=reg.get_api_key(p.id), default_model=m.model_id,
            )
    return None


# ---------------------------------------------------------------------------
# Slot-id resolvers for the voice subsystem.
#
# Generalizes the chat/embeddings precedent (resolve_chat_model /
# resolve_embedding_model) to the transcription / tts / voice / realtime base
# paths: each returns the model id assigned to its capability slot, or the
# caller-supplied fallback (normally None). Per the no-hardcoded-models rule, an
# unassigned slot returns None and the caller must treat the capability as "not
# configured" — never substitute a hardcoded default.
# ---------------------------------------------------------------------------
def _resolve_slot_model_id(db: Session, slot: str) -> Optional[str]:
    try:
        resolved = ProviderRegistryService(db).resolve_slot(slot)
        if resolved and resolved.model_id:
            return resolved.model_id
    except Exception as exc:  # noqa: BLE001 — never break the voice path
        log.warningx("Slot model resolutie mislukt", slot=slot, error=str(exc))
    return None


def resolve_transcription_model(db: Session, model: Optional[str] = None) -> Optional[str]:
    """Effective transcription (STT) model id from the 'transcription' slot."""
    return _resolve_slot_model_id(db, "transcription") or model


def resolve_tts_model(db: Session, model: Optional[str] = None) -> Optional[str]:
    """Effective text-to-speech model id from the 'tts' slot."""
    return _resolve_slot_model_id(db, "tts") or model


def resolve_voice_model(db: Session, model: Optional[str] = None) -> Optional[str]:
    """Effective voice-conversation model id from the 'voice' slot."""
    return _resolve_slot_model_id(db, "voice") or model


def resolve_realtime_model(db: Session, model: Optional[str] = None) -> Optional[str]:
    """Effective realtime-conversation model id from the 'realtime' slot."""
    return _resolve_slot_model_id(db, "realtime") or model


def resolve_default_chat_model(db: Session, model: Optional[str] = None) -> Optional[str]:
    """First assigned chat-slot model id, for call sites that hit the OpenAI base
    path directly (e.g. the voice services) instead of going through the
    LLMRouter. Mirrors resolve_chat_model's fallback order. Returns None when no
    chat slot is assigned — the caller must then treat chat as not configured."""
    for slot in ("chat.planner", "chat.cognition"):
        mid = _resolve_slot_model_id(db, slot)
        if mid:
            return mid
    return model


def resolve_openai_transcription_model(db: Session) -> Optional[str]:
    """Model id of the 'transcription' slot only when it is OpenAI-backed.

    The recording/live transcription paths call the OpenAI client with
    OpenAI-specific request features (diarized_json, word timestamps), so a
    non-OpenAI transcription model cannot serve them. Returns None when the slot
    is unassigned or assigned to a non-OpenAI provider, so the caller gates the
    OpenAI-specific feature off cleanly instead of using a hardcoded model."""
    try:
        resolved = ProviderRegistryService(db).resolve_slot("transcription")
    except Exception as exc:  # noqa: BLE001 — never break the voice path
        log.warningx("Transcription slot resolutie mislukt", error=str(exc))
        return None
    if not resolved or (resolved.provider_type or "") != "openai":
        return None
    return resolved.model_id or None
