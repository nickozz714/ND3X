"""
services/voice/meeting_profile_ai.py

"Generate with AI" for meeting profiles: from a short wizard (meeting type,
goals, language, desired output, optional live-action wishes) an LLM builds a
complete profile (instructions + markdown output template + optional
action_policy). The model comes from a routing slot — no hardcoded model.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

_INSTRUCTIONS = (
    "You design meeting-assistant profiles for a live meeting transcription tool. "
    "Given the user's wizard answers, produce ONE JSON object with these fields:\n"
    "- name: short profile name\n"
    "- description: one sentence\n"
    "- instructions: detailed guidance for the live assistant (what to capture, "
    "tone, structure, what to ignore) — several sentences, actionable\n"
    "- language: BCP-47-ish code or plain language name the user asked for\n"
    "- output_template: a markdown skeleton for the meeting notes with clear "
    "headings and, where useful, tables (use markdown). This is what the finished "
    "notes look like.\n"
    "- action_policy: EITHER null (no live actions) OR an object "
    "{enabled:true, autonomy:'suggest', allowed_actions:['web_search'], "
    "keywords:[...], max_per_tick:1} when the user wants live look-ups.\n"
    "Return JSON only — no markdown fences, no commentary."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "instructions": {"type": "string"},
        "language": {"type": ["string", "null"]},
        "output_template": {"type": "string"},
        "action_policy": {"type": ["object", "null"]},
    },
    "required": ["name", "instructions", "output_template"],
}


def resolve_profile_ai_model(db: Session) -> Optional[str]:
    """Model for profile generation: the per-wizard slot (wizard.meeting_profile)
    when set, else the shared wizard slot (wizard.generator). NO fallback to
    other slots — both empty → None → the wizard is off."""
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    for slot in ("wizard.meeting_profile", "wizard.generator"):
        try:
            r = reg.resolve_slot(slot)
        except Exception:  # noqa: BLE001
            r = None
        if r and getattr(r, "model_id", None):
            return r.model_id
    return None


def _build_prompt(answers: Dict[str, Any]) -> str:
    lines: List[str] = ["Wizard answers:"]
    for key, label in (
        ("meeting_type", "Meeting type"),
        ("goals", "Goals / what matters"),
        ("language", "Language"),
        ("output", "Desired output / notes style"),
        ("participants", "Participants / roles"),
        ("live_actions", "Live actions wanted (e.g. auto web look-ups)"),
        ("extra", "Anything else"),
    ):
        val = (answers or {}).get(key)
        if val:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines)


async def generate_profile(db: Session, answers: Dict[str, Any]) -> Dict[str, Any]:
    """Return a draft profile dict (name/description/instructions/language/
    output_template/action_policy). Raises RuntimeError with an actionable
    message when no model is configured or the model output can't be parsed."""
    model = resolve_profile_ai_model(db)
    if not model:
        raise RuntimeError(
            "No model is assigned to generate profiles. Assign one to the "
            "AI wizards slot (or the Meeting profile wizard slot) under AI Models → Routing."
        )

    from services.openai_service import OpenAIResponsesService
    from services.providers.provider_factory import build_llm_router
    router = build_llm_router(OpenAIResponsesService(), db)
    resp = await router.ask_orchestration_async(
        _build_prompt(answers),
        role="cognition:meeting_profile_generator",
        model=model,
        instructions=_INSTRUCTIONS,
        keep_context=False,
        store=False,
        session_id=None,
        max_output_tokens=2000,
        json_schema=_SCHEMA,
    )
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise RuntimeError(f"The model ({model}) returned no output.")
    try:
        data = _extract_json(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not parse the model's profile draft: {exc}")

    return {
        "name": (data.get("name") or "").strip() or "AI meeting profile",
        "description": data.get("description"),
        "instructions": data.get("instructions"),
        "language": data.get("language"),
        "output_template": data.get("output_template"),
        "action_policy": data.get("action_policy") if isinstance(data.get("action_policy"), dict) else None,
        "_model": model,
    }


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        # strip a ```json ... ``` fence if the model added one
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return obj
