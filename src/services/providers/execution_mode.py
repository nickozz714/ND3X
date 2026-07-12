"""
services/providers/execution_mode.py

Agent-mode framework primitives: which execution mode a routing slot runs in,
and which slots may be outsourced to a CLI agent at all.

The two execution modes (WHY they exist):
- "model" — orchestrator-native. The orchestrator drives the LLM through its
  own multi-step logic (planner loop, structured pipelines) and enforces
  structured output via response_format/json_schema. This is the classic path
  and works for every plain chat model.
- "agent" — CLI-delegated. The slot resolves to a CLI-agent provider
  (`ChatProvider.is_cli_agent = True`, e.g. Claude Code): that provider runs
  its OWN agent loop with its own tools; ND3X hands it skills/MCP/tools via the
  gateway and receives the result through an output contract (a tolerantly
  parsed envelope). Structured output cannot be enforced — CLI agents have
  `supports_structured_output = False` and ignore response_format — so
  subsystems that need JSON must use an envelope contract, not a schema.

No fallbacks (core principle): the slot assignment IS the configuration.
- Empty slot → the step simply does not run (feature off).
- CLI agent on a slot → agent mode runs (an envelope-based path must exist).
- Plain model on a slot → model mode runs.
Modality/realtime slots (embeddings, TTS/STT, live, image) have no CLI-agent
interface at all; a CLI agent is rejected at ASSIGNMENT time, never silently
substituted at runtime.
"""
from __future__ import annotations

from typing import Dict, Literal, Optional

from sqlalchemy.orm import Session

from services.providers.base import ChatProvider

# Ensure CLI-agent provider classes are registered on ChatProvider's type
# registry (registration happens at class definition, i.e. on module import).
# A future CLI-agent provider (Codex, ...) must be imported here too — the
# authoritative flag stays on the class; this import only guarantees the class
# is known when a lookup happens by type string before any instance was built.
import services.providers.claude_code_provider  # noqa: F401  (registers claude_code)

SlotMode = Literal["agent", "model"]

# Capability classes: can this slot's work be outsourced to a CLI agent?
OUTSOURCEABLE = "outsourceable"  # text/reasoning/deciding — agent mode allowed
MODALITY = "modality"            # modality/realtime — orchestrator-only, CLI agent not assignable

#: Capability class per routing slot (see capability_router.ALL_SLOTS).
CAP_CLASS: Dict[str, str] = {
    # text / reasoning / deciding → may run as a CLI agent
    "chat.planner": OUTSOURCEABLE,
    "chat.cognition": OUTSOURCEABLE,
    "chat.memory_decision": OUTSOURCEABLE,
    "chat.auto_decision": OUTSOURCEABLE,
    "meeting.action_detector": OUTSOURCEABLE,
    "wizard.generator": OUTSOURCEABLE,
    "wizard.skill": OUTSOURCEABLE,
    "wizard.workflow": OUTSOURCEABLE,
    "wizard.meeting_profile": OUTSOURCEABLE,
    # modality / realtime → no CLI-agent interface exists; orchestrator-only
    "embeddings": MODALITY,
    "transcription": MODALITY,
    "tts": MODALITY,
    "voice": MODALITY,
    "realtime": MODALITY,
    "image_generation": MODALITY,
}


def capability_class(slot: Optional[str]) -> Optional[str]:
    """OUTSOURCEABLE / MODALITY for a known slot, None for an unknown one."""
    return CAP_CLASS.get((slot or "").strip())


def is_cli_agent_type(provider_type: Optional[str]) -> bool:
    """Whether a provider_type string denotes a CLI-agent provider — derived
    from the class's `is_cli_agent` capability, never from name matching."""
    cls = ChatProvider.class_for_type(provider_type)
    return bool(getattr(cls, "is_cli_agent", False))


def slot_mode(db: Session, slot: str) -> Optional[SlotMode]:
    """The execution mode a routing slot runs in right now.

    Returns "agent" when the slot resolves to an enabled CLI-agent provider,
    "model" when it resolves to a plain model, and None when the slot is
    unassigned/disabled — in which case the step must simply not run (the
    no-fallback rule; never substitute another model)."""
    from services.providers.registry_service import ProviderRegistryService

    try:
        resolved = ProviderRegistryService(db).resolve_slot(slot)
    except Exception:  # noqa: BLE001 — a mode probe must never break the caller
        return None
    if resolved is None:
        return None
    return "agent" if is_cli_agent_type(resolved.provider_type) else "model"
