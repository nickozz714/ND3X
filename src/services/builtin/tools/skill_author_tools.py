"""
services/builtin/tools/skill_author_tools.py

Builtin tools that let the assistant (the LLM) AUTHOR its own ND3X skills — write
a SKILL.md body (plus optional extra files) straight into the skill catalog, so a
reusable capability learned in a conversation becomes a first-class, selectable
skill. Source-agnostic: the content may be composed by the model, or fetched from
any external system (reached as a normal MCP tool) and then written here.

- skill__create : create (or update in place) a skill from content
- skill__list   : list the skills already in the ND3X catalog

Registered on import (imported in ask_job_callbacks.py).
"""
from __future__ import annotations

from typing import Any, Dict, List

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


@internal_tool_registry.register(
    name="skill__create",
    title="Create Skill",
    description=(
        "Author a new ND3X skill (or update one you authored earlier) from content. "
        "Use this to turn a reusable way-of-working you've established into a first-class, "
        "selectable skill. 'instructions' is the skill body (its SKILL.md) — write it as "
        "clear guidance the assistant should follow when the skill is selected. Optionally "
        "attach extra files. The skill is enabled by default so it's selectable next turn. "
        "Re-running with the same title updates the skill in place."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Human-readable skill title (the name is derived from it)"},
            "instructions": {"type": "string", "description": "The skill body / SKILL.md — the guidance to follow when the skill is used"},
            "description": {"type": "string", "description": "Short one-line description used when matching the skill to a task"},
            "files": {
                "type": "array",
                "description": "Optional extra files bundled with the skill",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path (e.g. resources/example.py); SKILL.md is taken from 'instructions'"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            "enable": {"type": "boolean", "description": "Enable the skill after creating it so it is immediately selectable (default true)"},
        },
        "required": ["title", "instructions"],
    },
    tags=["internal", "skill"],
)
async def skill_create(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from services.assistants.skill_service import SkillService

    a = args or {}
    title = (a.get("title") or "").strip()
    instructions = (a.get("instructions") or "").strip()
    if not title or not instructions:
        return {"status": "error", "error": "Both 'title' and 'instructions' are required."}
    files = a.get("files") if isinstance(a.get("files"), list) else []
    enable = a.get("enable", True)

    with SessionLocal() as db:
        try:
            result = SkillService(db).write_skill_from_content(
                title=title,
                instructions=instructions,
                description=(a.get("description") or "").strip(),
                files=files,
                source="authored",
                enable=bool(enable),
            )
        except Exception as exc:  # noqa: BLE001 — includes the 409 name-clash case
            detail = getattr(exc, "detail", None) or str(exc)
            log.warningx("skill__create failed", error=str(detail))
            return {"status": "error", "error": str(detail)[:300]}

        return {
            "status": "success",
            **result,
            "note": (
                f"Skill '{result['name']}' "
                + ("created" if result["created"] else "updated")
                + ". Start a new turn for it to be selectable."
            ),
        }


@internal_tool_registry.register(
    name="skill__list",
    title="List Skills",
    description="List the skills already in the ND3X catalog (name, description, source) so you can reuse or update one instead of creating a duplicate.",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "skill"],
)
async def skill_list(_args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from services.assistants.skill_service import SkillService

    with SessionLocal() as db:
        skills = SkillService(db).get_all(include_disabled=True)
        out: List[Dict[str, Any]] = [
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": (s.description or "")[:200],
                "source": s.source,
                "enabled": bool(s.is_enabled),
            }
            for s in skills
        ]
        return {"status": "success", "count": len(out), "skills": out}
