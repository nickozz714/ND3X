"""
models/provider.py

Database registry for the provider/model-agnostic AI platform.

- Provider:            a configured supplier (openai, anthropic, openai_compatible,
                       ollama, gemini, ...). API keys are stored ENCRYPTED in the DB
                       (Fernet via utils.crypto) — credentials are DB settings, not env.
- ProviderModel:       a model offered by a provider for a given capability
                       (chat / embeddings / transcription / tts / realtime).
- CapabilityAssignment: which ProviderModel is active for a routing slot
                       (chat.planner / chat.cognition / chat.auto_decision /
                        embeddings / transcription / voice / ...).
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.database import Base


# Provider type identifiers (kept as plain strings for forward-compat).
PROVIDER_TYPES = (
    "openai",
    "anthropic",
    "openai_compatible",
    "ollama",
    "gemini",
    "voyage",
    # Headless Claude Code CLI on the local machine; auth = `claude setup-token`
    # OAuth token (subscription), stored in api_key_encrypted like other keys.
    "claude_code",
    # Azure AI Foundry (Microsoft Foundry Models) via the v1 OpenAI-compatible
    # route (https://<resource>.openai.azure.com/openai/v1). model_id = the
    # DEPLOYMENT name; auth = Azure API key in api_key_encrypted.
    "azure_foundry",
)

# Capability identifiers a ProviderModel can serve.
CAPABILITIES = ("chat", "embeddings", "transcription", "tts", "realtime", "image_generation")

# Routing slots a CapabilityAssignment can fill (authoritative list lives in
# services/providers/capability_router.ALL_SLOTS; this mirrors it for docs).
ROUTING_SLOTS = (
    "chat.planner",
    "chat.cognition",
    "chat.background",
    "chat.auto_decision",
    "meeting.action_detector",
    "embeddings",
    "transcription",
    "tts",
    "voice",
    "realtime",
)


class Provider(Base):
    __tablename__ = "providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    provider_type = Column(String(64), nullable=False, index=True)
    base_url = Column(String(512), nullable=True)
    # Fernet-encrypted API key; never store or return the plaintext.
    api_key_encrypted = Column(Text, nullable=True)
    # Fernet-encrypted Admin/usage key for the provider's billing/usage API
    # (e.g. OpenAI/Anthropic org usage & cost). Optional; never returned plaintext.
    admin_api_key_encrypted = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    is_local = Column(Boolean, nullable=False, default=False)
    # Optional JSON-encoded extra config (org id, headers, ollama host, ...).
    config_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    models = relationship(
        "ProviderModel",
        back_populates="provider",
        cascade="all, delete-orphan",
    )


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", "capability", name="uq_provider_model_capability"),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id = Column(String(255), nullable=False)
    capability = Column(String(32), nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    context_window = Column(Integer, nullable=True)
    price_in = Column(Float, nullable=True)   # $ per 1M input tokens
    price_out = Column(Float, nullable=True)  # $ per 1M output tokens
    good_for = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    is_local = Column(Boolean, nullable=False, default=False)
    # Per-model override for native web search support (None → curated default by
    # provider/model-family; see services/providers/web_search_capability.py).
    supports_web_search = Column(Boolean, nullable=True)
    # Per-model override for vision/image input (None → curated default by
    # provider/model-family; see services/providers/vision_capability.py).
    supports_vision = Column(Boolean, nullable=True)
    # Per-model: append the "extra guidance" instruction block for less-capable
    # models (skill-vs-tool primer, step discipline). None/False = off. Toggled in
    # AI Models → Routing per model; can also be overridden per chat session.
    needs_extra_guidance = Column(Boolean, nullable=True)
    # Per-model planner prompt mode: "full" | "light" | None → auto (light when
    # the provider/model is local). Light mode sends a compact planner prompt —
    # small models are prefill-bound, so prompt size dominates step latency.
    prompt_mode = Column(String(16), nullable=True)
    # How many turns ND3X runs concurrently on this LOCAL model before showing
    # the queue indicator; should match the Ollama server's OLLAMA_NUM_PARALLEL.
    # None → 1. Cloud models handle concurrency themselves.
    num_parallel = Column(Integer, nullable=True)
    # For local models: not_deployed | deploying | ready | error
    deploy_state = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    provider = relationship("Provider", back_populates="models")


class CapabilityAssignment(Base):
    __tablename__ = "capability_assignments"

    id = Column(Integer, primary_key=True, index=True)
    slot = Column(String(64), unique=True, nullable=False, index=True)
    provider_model_id = Column(
        Integer,
        ForeignKey("provider_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    provider_model = relationship("ProviderModel")
