"""
services/providers/capability_router.py

Routing is authoritative: a capability is usable only when its slot has a model
assigned. There is no silent fallback.

- Optional capabilities (cognition, voice/STT/TTS/realtime): when unassigned the
  feature is simply disabled and skipped by the orchestrator/endpoints.
- Required capabilities (chat, embeddings): when unassigned, attempting to use
  them raises CapabilityNotConfigured so processing stops with a clear message.
"""
from __future__ import annotations

import logging
from typing import Optional, Set

from sqlalchemy.orm import Session

from services.providers.registry_service import ProviderRegistryService

logger = logging.getLogger(__name__)

# Single-agent model: one chat model runs the agent — skill choice, the planner
# loop, and the final answer it writes all happen inside that one loop. The legacy
# chat.router / chat.final_answer / chat.selection slots are retired (skill choice
# is now an in-loop action on chat.planner, not a separate model call).
CHAT_SLOTS = ("chat.planner",)

# Every routing slot the orchestrator/voice subsystem can resolve. Chat and
# embeddings are required; the rest are optional (feature disabled when unset).
ALL_SLOTS = (
    "chat.planner",
    "chat.cognition",
    # Background agents (agent__dispatch / task__create): the model that drives a
    # dispatched/background subagent run, SEPARATE from the foreground planner so a
    # background job can use a different (e.g. cheaper, or truly-parallel cloud)
    # model. NO FALLBACK: unassigned → dispatch is refused with a clear error (the
    # assignment is the configuration). A CLI-agent here makes background runs run
    # in agent mode. A per-call `model` on dispatch overrides this slot.
    "chat.background",
    # Memory-retrieval decision (advanced, optional): assigned → that model
    # decides per turn whether to retrieve memories; UNASSIGNED → the decision
    # step is OFF entirely (no fallback to the planner model).
    "chat.memory_decision",
    "chat.auto_decision",
    "meeting.action_detector",
    # AI "Generate with AI" wizards. wizard.generator is the shared default used
    # by ALL wizards (Simple mode); the per-wizard slots override it (Advanced).
    # Each wizard resolves: its own slot → wizard.generator → cognition/planner.
    "wizard.generator",
    "wizard.skill",
    "wizard.workflow",
    "wizard.meeting_profile",
    "embeddings",
    "transcription",
    "tts",
    "voice",
    "realtime",
    # Image generation (optional): assigned → image__generate works with that
    # model/provider; unassigned → the feature is off. Only providers that can
    # actually generate (openai / gemini / openai_compatible) resolve here.
    "image_generation",
)


class CapabilityNotConfigured(Exception):
    """Raised when a required capability (chat/embeddings) has no assigned model."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(
            f"{capability} has no model assigned. Assign one under "
            f"AI Models → Routing before using it."
        )


def assigned_slots(db: Session) -> Set[str]:
    """Slots that currently have a model assigned."""
    out: Set[str] = set()
    try:
        for a in ProviderRegistryService(db).list_assignments():
            if getattr(a, "provider_model_id", None):
                out.add(a.slot)
    except Exception:  # noqa: BLE001 — never break callers on a registry hiccup
        return set()
    return out


def compute_capabilities(db: Session) -> dict:
    """Enabled-capability map derived from the current slot assignments."""
    s = assigned_slots(db)
    return {
        "chat": any(x in s for x in CHAT_SLOTS),
        "embeddings": "embeddings" in s,
        "cognition": "chat.cognition" in s,
        "transcription": "transcription" in s,
        "tts": "tts" in s,
        "voice": "voice" in s,
        "realtime": "realtime" in s,
        "_assigned": sorted(s),
    }


def resolved_models(db: Session) -> dict[str, Optional[str]]:
    """Map every known slot to its resolved ``provider_type:model_id`` string,
    or ``None`` when the slot has no usable (enabled) model assigned."""
    out: dict[str, Optional[str]] = {slot: None for slot in ALL_SLOTS}
    try:
        for a in ProviderRegistryService(db).list_assignments():
            if a.slot in out and getattr(a, "model_id", None):
                provider = a.provider_type or "?"
                out[a.slot] = f"{provider}:{a.model_id}"
    except Exception:  # noqa: BLE001 — never break startup on a registry hiccup
        logger.exception("Failed to resolve models per slot")
    return out


def log_resolved_models(db: Session) -> dict[str, Optional[str]]:
    """Log the resolved model for each routing slot (called at startup).

    Required slots (chat.*, embeddings) without a model are flagged WARNING so a
    misconfigured deployment is obvious; optional slots log at INFO.
    """
    resolved = resolved_models(db)
    logger.info("Routing slots → resolved models:")
    required = set(CHAT_SLOTS) | {"embeddings"}
    for slot in ALL_SLOTS:
        model = resolved.get(slot)
        if model:
            logger.info("  %-18s → %s", slot, model)
        elif slot in required:
            logger.warning("  %-18s → (not assigned — capability unavailable)", slot)
        else:
            logger.info("  %-18s → (not assigned — feature disabled)", slot)
    return resolved
