"""
services/assistants/skill_ai.py

"Generate with AI" for skills: from a short wizard (what the skill should do,
when the agent should use it, which tools/data it needs) an LLM writes a
complete skill draft (name, description, instructions, routing tags). The model
comes from a routing slot — no hardcoded model.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

_INSTRUCTIONS = (
    "You author SKILLS for an AI agent platform. A skill is a reusable capability the agent "
    "loads when a user's request matches it: it carries INSTRUCTIONS (how to do the task well) "
    "and is linked to tools. Given the user's wizard answers, produce ONE JSON object:\n"
    "- name: a short lowercase snake_case identifier (letters/digits/underscore), e.g. "
    "'invoice_processing'\n"
    "- display_name: a human title, e.g. 'Invoice processing'\n"
    "- description: one clear sentence — WHAT it does + WHEN the agent should use it (this drives "
    "skill selection)\n"
    "- instructions: detailed, actionable guidance for the agent on how to carry out the task "
    "step by step, what to check, what to avoid, and how to report results. Several paragraphs.\n"
    "- routing_tags: a short array of lowercase tags for filtering (e.g. ['finance','documents'])\n"
    "- suggested_tools: a short array of capabilities the skill likely needs, in plain words "
    "(e.g. ['read documents','web search']) — advisory only, the user links real tools later.\n"
    "Return JSON only — no markdown fences, no commentary."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "display_name": {"type": "string"},
        "description": {"type": "string"},
        "instructions": {"type": "string"},
        "routing_tags": {"type": "array", "items": {"type": "string"}},
        "suggested_tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "description", "instructions"],
}


def resolve_skill_ai_model(db: Session) -> Optional[str]:
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    # Per-wizard slot, else the shared wizard slot. NO other fallback.
    for slot in ("wizard.skill", "wizard.generator"):
        try:
            r = reg.resolve_slot(slot)
        except Exception:  # noqa: BLE001
            r = None
        if r and getattr(r, "model_id", None):
            return r.model_id
    return None


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    return s or "new_skill"


def _build_prompt(answers: Dict[str, Any]) -> str:
    lines: List[str] = ["Wizard answers:"]
    for key, label in (
        ("purpose", "What the skill should do"),
        ("when", "When the agent should use it"),
        ("tools", "Tools / data it needs"),
        ("steps", "Key steps or rules"),
        ("output", "Expected output"),
        ("extra", "Anything else"),
    ):
        val = (answers or {}).get(key)
        if val:
            lines.append(f"- {label}: {val}")
    # A skill that IS a workflow: instruct the model to write instructions that
    # kick off the given workflow via the workflow__run tool.
    wf = (answers or {}).get("workflow")
    if wf:
        lines.append(
            f"- This skill should RUN a workflow named '{wf}'. Write the instructions so the "
            f"agent uses the workflow__run tool with workflow='{wf}' (and passes relevant input) "
            "to kick it off, then reports the run id. Mention workflow__list if the name is unclear."
        )
    return "\n".join(lines)


async def generate_skill(db: Session, answers: Dict[str, Any]) -> Dict[str, Any]:
    """Return a draft skill dict. Raises RuntimeError with an actionable message
    when no model is configured or the output can't be parsed."""
    model = resolve_skill_ai_model(db)
    if not model:
        raise RuntimeError(
            "No model is assigned to generate skills. Assign one to the "
            "AI wizards slot (or the Skill wizard slot) under AI Models → Routing."
        )

    from services.openai_service import OpenAIResponsesService
    from services.providers.provider_factory import build_llm_router
    router = build_llm_router(OpenAIResponsesService(), db)
    resp = await router.ask_orchestration_async(
        _build_prompt(answers),
        role="cognition:skill_generator",
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
        raise RuntimeError(f"Could not parse the model's skill draft: {exc}")

    tags = data.get("routing_tags")
    return {
        "name": _slugify(data.get("name") or data.get("display_name") or "new_skill"),
        "display_name": (data.get("display_name") or "").strip() or None,
        "description": (data.get("description") or "").strip(),
        "instructions": (data.get("instructions") or "").strip(),
        "routing_tags": [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else [],
        "suggested_tools": [str(t).strip() for t in (data.get("suggested_tools") or []) if str(t).strip()]
        if isinstance(data.get("suggested_tools"), list) else [],
        "_model": model,
    }


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
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
