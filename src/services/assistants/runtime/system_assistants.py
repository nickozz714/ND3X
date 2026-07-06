"""
services/assistants/runtime/system_assistants.py

Hardcoded (code-authoritative) response schemas en instructies voor de
system-assistants. Deze worden NIET meer uit de database gelezen/aangepast:

  * response schema  -> hardcoded voor router, planner én final_answer
  * instructie       -> hardcoded voor router en final_answer
                        (planner-instructies blijven bewerkbaar via de DB/UI)

De specificaties staan in ./system_specs/ en worden bij import één keer geladen.
``apply_system_overrides`` past ze toe op een AssistantConfig in
runtime_config_loader, het enige punt waar configs worden opgebouwd.

Daarnaast levert deze module de "capabilities primer": een vaste, niet door
gebruikers te bewerken prompt-sectie die agents leert over parallelle tool
calls, subagent-dispatch en achtergrondtaken.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from component.logging import get_logger
from services.assistants.runtime_config import AssistantConfig

log = get_logger(__name__)

_SPEC_DIR = Path(__file__).parent / "system_specs"


def _load_json(name: str) -> Dict[str, Any]:
    return json.loads((_SPEC_DIR / name).read_text(encoding="utf-8"))


def _load_text(name: str) -> str:
    return (_SPEC_DIR / name).read_text(encoding="utf-8").strip()


# Schemas per type (code-authoritative voor alle drie).
_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "router": _load_json("router.schema.json"),
    "planner": _load_json("planner.schema.json"),
    "final_answer": _load_json("final_answer.schema.json"),
}

# Instructies die hardcoded zijn (alleen router + final_answer).
_INSTRUCTIONS: Dict[str, str] = {
    "router": _load_text("router.instruction.md"),
    "final_answer": _load_text("final_answer.instruction.md"),
}

CAPABILITIES_PRIMER: str = _load_text("capabilities_primer.md")

# Optional, user-toggled "for dummies" guidance (settings.AGENT_EXTRA_GUIDANCE).
# Appended to the agent/planner prompt to help less-capable models with the
# skill-vs-tool distinction, step discipline, and exact-name copying.
EXTRA_GUIDANCE_PRIMER: str = _load_text("agent.instruction.guidance.md")

# Light mode: distilled core contract that replaces the verbose orchestrator_*
# system-skill contracts in the planner prompt for small/local models (prompt
# size dominates their step latency). See docs/light-mode.md for what is
# shortened or omitted versus full mode.
LIGHT_MODE_CONTRACT: str = _load_text("agent.instruction.light.md")

# Goal mode (/goal): keep working until the goal is DEMONSTRABLY achieved (or
# demonstrably unachievable) — evidence from tool results, never assumptions.
# Prepended to the planner prompt when payload["_goal_mode"] is set.
GOAL_MODE_CONTRACT: str = _load_text("agent.instruction.goal.md")

# Korte awareness-notitie voor de router: de router roept zelf geen tools aan,
# maar plant stappen; downstream planners kunnen parallelle tools/subagents/
# achtergrondtaken gebruiken.
_ROUTER_CAPABILITIES_NOTE = (
    "## Downstream execution awareness\n"
    "Planner assistants you route to can execute multiple tool calls in parallel, "
    "dispatch subagents (agent__dispatch), and run background tasks (task__create). "
    "Scope each step so a capable planner can carry it out end to end."
)


def schema_for_type(assistant_type: str) -> Optional[Dict[str, Any]]:
    """Hardcoded schema voor dit type, of None als er geen override is."""
    schema = _SCHEMAS.get((assistant_type or "").strip())
    # Kopie zodat downstream-mutaties de gedeelde spec niet aanpassen.
    return json.loads(json.dumps(schema)) if schema is not None else None


def instruction_override_for_type(assistant_type: str) -> Optional[str]:
    """Hardcoded instructie voor router/final_answer, of None (bv. planner)."""
    return _INSTRUCTIONS.get((assistant_type or "").strip())


def capabilities_primer_for_type(assistant_type: str) -> str:
    """Prompt-sectie die de agent leert over de nieuwe uitvoeringsmogelijkheden."""
    t = (assistant_type or "").strip()
    if t == "router":
        return _ROUTER_CAPABILITIES_NOTE
    if t == "planner":
        return CAPABILITIES_PRIMER
    return ""


def apply_system_overrides(config: AssistantConfig) -> AssistantConfig:
    """Forceer code-authoritative schema/instructie op een AssistantConfig.

    Muteert en retourneert dezelfde config. Schema wordt altijd overschreven voor
    de drie system-types; instructie alleen voor router en final_answer.
    """
    atype = (getattr(config, "assistant_type", "") or "").strip()

    schema = schema_for_type(atype)
    if schema is not None:
        config.schema = schema

    instruction = instruction_override_for_type(atype)
    if instruction is not None:
        config.instruction = instruction

    return config
