"""
services/providers/nd3x_agent_context.py

Dynamic ND3X inventory that augments the STATIC ``ND3X_AGENT_PREAMBLE`` for a
CLI-agent turn (chat or workflow). The preamble tells the agent *that* ND3X's
capabilities live under ``mcp__nd3x__``; this block tells it *which* ones are
actually live right now, straight from the DB — so it stays correct when the user
connects a new MCP server or adds a skill, instead of the preamble's hardcoded
"e.g. Fabric" going stale.

It deliberately does NOT re-list individual tools: the CLI agent already discovers
those via the MCP gateway (name + description + schema). What MCP discovery does
NOT give is the SKILL layer and where a skill's files live — that's what this adds:
- connected MCP servers by name,
- the skill catalog (enabled, non-system/non-runtime): name — description,
- the on-disk root of the turn's SELECTED file-backed skills, so the agent's own
  Read/Bash can use their scripts.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)


def build_nd3x_context_block(db: Session, *, selected_skill_names: Optional[List[str]] = None) -> str:
    """Return the dynamic ND3X inventory block, or '' when there's nothing to add.
    Never raises — a context probe must not break the turn."""
    parts: List[str] = []

    # Connected MCP servers (by name) — the builtin server is implicit, skip it.
    try:
        from models.mcp_server import MCPServer
        servers = (db.query(MCPServer)
                   .filter(MCPServer.is_enabled == True)  # noqa: E712
                   .order_by(MCPServer.name.asc())
                   .all())
        names = [s.name for s in servers
                 if s.name and (getattr(s, "server_type", "") or "").lower() != "builtin"]
        if names:
            parts.append(
                "Connected ND3X MCP servers (their tools appear as mcp__nd3x__*): "
                + ", ".join(names) + ".")
    except Exception as exc:  # noqa: BLE001
        log.debugx("nd3x context: MCP-servers overslaan", error=str(exc))

    # Skill catalog — selectable domain skills only (not system/runtime).
    try:
        from models.skill import Skill
        rows = (db.query(Skill)
                .filter(Skill.is_enabled == True,   # noqa: E712
                        Skill.is_system == False,    # noqa: E712
                        Skill.is_runtime == False)   # noqa: E712
                .order_by(Skill.name.asc())
                .all())
        lines = [f"- {s.name}: {(s.description or '').strip()}" for s in rows if s.name]
        if lines:
            parts.append(
                "Available ND3X skills (their tools are mcp__nd3x__*; use the relevant one "
                "when it fits the task):\n" + "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        log.debugx("nd3x context: skill-catalogus overslaan", error=str(exc))

    # File roots for the turn's SELECTED file-backed skills.
    sel = [str(n).strip() for n in (selected_skill_names or []) if str(n).strip()]
    if sel:
        try:
            from models.skill import Skill
            from services.assistants.skill_file_service import SkillFileService
            svc = SkillFileService(db)
            froots: List[str] = []
            for s in db.query(Skill).filter(Skill.name.in_(sel)).all():
                try:
                    if getattr(s, "files", None):  # only skills that actually have files
                        froots.append(f"- {s.name}: {svc.runtime_root_for(int(s.id))}")
                except Exception:  # noqa: BLE001
                    pass
            if froots:
                parts.append(
                    "Selected skills with files on disk — read/run them with your own Read/Bash "
                    "tools:\n" + "\n".join(froots))
        except Exception as exc:  # noqa: BLE001
            log.debugx("nd3x context: skill-files overslaan", error=str(exc))

    return "\n\n".join(parts)
