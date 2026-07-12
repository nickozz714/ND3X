"""schemas/provider.py — request/response models for the provider registry.

API keys are write-only: accepted on create/update, never returned. Responses
expose only `has_api_key`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ProviderBase(BaseModel):
    name: str
    provider_type: str
    base_url: Optional[str] = None
    enabled: bool = True
    is_local: bool = False
    config_json: Optional[str] = None


class ProviderCreate(ProviderBase):
    api_key: Optional[str] = None  # plaintext in, encrypted at rest, never returned
    admin_api_key: Optional[str] = None  # billing/usage admin key; encrypted at rest


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    is_local: Optional[bool] = None
    config_json: Optional[str] = None
    api_key: Optional[str] = None  # set to update; omit to keep existing
    admin_api_key: Optional[str] = None  # set to update; "" clears; omit to keep existing


class ProviderRead(ProviderBase):
    id: int
    has_api_key: bool = False
    has_admin_key: bool = False  # whether a billing/usage admin key is stored
    # "none" | "ok" | "unreadable" — "unreadable" means a key is stored but can't
    # be decrypted (the encryption key changed since it was saved → re-enter it).
    key_status: str = "none"


class ProviderModelBase(BaseModel):
    model_id: str
    capability: str
    display_name: Optional[str] = None
    context_window: Optional[int] = None
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    good_for: Optional[str] = None
    enabled: bool = True
    is_local: bool = False
    deploy_state: Optional[str] = None
    # Per-model native-web-search override (None → curated default by family).
    supports_web_search: Optional[bool] = None
    # Per-model vision/image-input override (None → curated default by family).
    supports_vision: Optional[bool] = None
    # Per-model: append the "extra guidance" instruction block (less-capable models).
    needs_extra_guidance: Optional[bool] = None
    # Planner prompt mode: "full" | "light" | None → auto (light when local).
    prompt_mode: Optional[str] = None
    # Concurrent-turn threshold for the local-model queue indicator (match
    # OLLAMA_NUM_PARALLEL). None → 1.
    num_parallel: Optional[int] = None


class ProviderModelCreate(ProviderModelBase):
    provider_id: int


class ProviderModelUpdate(BaseModel):
    display_name: Optional[str] = None
    context_window: Optional[int] = None
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    good_for: Optional[str] = None
    enabled: Optional[bool] = None
    deploy_state: Optional[str] = None
    # Manual capability override (e.g. a realtime model auto-classified as chat).
    capability: Optional[str] = None
    supports_web_search: Optional[bool] = None
    supports_vision: Optional[bool] = None
    needs_extra_guidance: Optional[bool] = None
    prompt_mode: Optional[str] = None
    num_parallel: Optional[int] = None


class ProviderModelRead(ProviderModelBase):
    id: int
    provider_id: int
    # Effective capability (override OR curated default) — what the UI badges.
    web_search_capable: bool = False
    vision_capable: bool = False


class CapabilityAssignmentRead(BaseModel):
    slot: str
    provider_model_id: Optional[int] = None
    # convenience denormalized fields for the UI
    provider_type: Optional[str] = None
    model_id: Optional[str] = None
    # execution mode for this slot: "agent" (CLI-agent provider — runs its own loop),
    # "model" (orchestrator-driven), or None (unassigned = the step is off). Lets the
    # routing UI show a per-slot mode badge.
    execution_mode: Optional[str] = None


class CapabilityAssignmentSet(BaseModel):
    slot: str
    provider_model_id: Optional[int] = None  # None clears the assignment


class ResolvedModel(BaseModel):
    """The resolution of a slot to a concrete provider+model."""
    slot: str
    provider_id: int
    provider_type: str
    base_url: Optional[str] = None
    model_id: str
    capability: str
    has_api_key: bool = False
