"""
services/import_export_service.py

Portable export/import for Skills, MCP Servers, Workflows and Meeting Profiles —
per item or in bulk. Produces a versioned JSON envelope:

    {"nd3x_export": 1, "kind": "<kind>", "exported_at": "<iso>", "items": [ ... ]}

Design choices for portability across installs:
- Only NON-secret, non-environment-specific fields are exported. MCP-server auth
  credentials are NEVER exported; MCP tools are re-synced from the server, so
  they are not exported either.
- References that would otherwise be raw ids are exported by NAME and re-resolved
  on import: workflow operations carry the target assistant/workflow name;
  skill→tool links carry (mcp_server_slug, remote_name).
- Import never overwrites: a name/slug clash gets a " (imported)" suffix, so
  importing is always additive and safe. Each item returns its own result.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)

EXPORT_VERSION = 1
KINDS = ("skill", "mcp_server", "workflow", "meeting_profile")


class ImportExportError(ValueError):
    pass


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Export ──────────────────────────────────────────────────────────────────

def export(db: Session, kind: str, ids: Optional[List[int]] = None) -> Dict[str, Any]:
    if kind not in KINDS:
        raise ImportExportError(f"Unknown export kind '{kind}'. Valid: {', '.join(KINDS)}")
    items = _EXPORTERS[kind](db, ids)
    return {
        "nd3x_export": EXPORT_VERSION,
        "kind": kind,
        "exported_at": _iso_now(),
        "items": items,
    }


def _export_meeting_profiles(db: Session, ids: Optional[List[int]]) -> List[Dict[str, Any]]:
    from services.voice.meeting_profile_service import MeetingProfileService
    rows = MeetingProfileService(db).list()
    out = []
    for p in rows:
        if ids and p.id not in ids:
            continue
        out.append({
            "name": p.name,
            "description": p.description,
            "instructions": p.instructions,
            "language": p.language,
            "output_template": p.output_template,
            "action_policy": p.action_policy,
            "enabled": p.enabled,
        })
    return out


def _export_workflows(db: Session, ids: Optional[List[int]]) -> List[Dict[str, Any]]:
    from services.workflows.workflow_service import WorkflowService
    from services.assistants.assistant_service import AssistantService
    assistants = AssistantService(db)
    rows = WorkflowService(db).get_all(limit=1000)
    out = []
    for wf in rows:
        if ids and wf.id not in ids:
            continue
        operations = list(getattr(wf, "operations", []) or [])
        # Cross-operation references (depends_on / follow-ups) are stored as
        # operation IDs, but create() expects POSITIONS (it re-resolves them to
        # the new ids). Map id → position so the export is portable.
        id_to_pos = {op.id: op.position for op in operations}

        def _pos(v):
            return id_to_pos.get(v, v)

        ops = []
        for op in sorted(operations, key=lambda o: o.position):
            ref_name = None
            if op.operation_type == "assistant":
                a = assistants.get_by_id(op.operation_ref_id) if hasattr(assistants, "get_by_id") else None
                ref_name = getattr(a, "name", None) if a else None
            ops.append({
                "name": op.name,
                "operation_type": op.operation_type,
                "ref_name": ref_name,  # resolved back to an id on import
                "config": op.config or {},
                "depends_on": [_pos(x) for x in (op.depends_on or [])],
                "on_success_follow_up": _pos(op.on_success_follow_up) if op.on_success_follow_up is not None else None,
                "on_failure_follow_up": _pos(op.on_failure_follow_up) if op.on_failure_follow_up is not None else None,
                "join_strategy": op.join_strategy,
                "timeout_seconds": op.timeout_seconds,
                "retry_policy": op.retry_policy or {},
                "position": op.position,
            })
        out.append({
            "name": wf.name,
            "description": wf.description,
            "input_schema": wf.input_schema or {},
            "schedule_cron": wf.schedule_cron,
            "is_enabled": wf.is_enabled,
            "operations": ops,
        })
    return out


def _export_mcp_servers(db: Session, ids: Optional[List[int]]) -> List[Dict[str, Any]]:
    from services.mcp.mcp_server_service import MCPServerService
    rows = MCPServerService(db).get_all(limit=1000)
    out = []
    for s in rows:
        if ids and s.id not in ids:
            continue
        # No auth/secrets; tools are re-synced from the server after import.
        out.append({
            "name": s.name,
            "slug": s.slug,
            "description": s.description,
            "server_type": s.server_type,
            "base_url": s.base_url,
            "stdio_command": s.stdio_command,
            "stdio_install_command": s.stdio_install_command,
            "is_enabled": s.is_enabled,
        })
    return out


def _export_skills(db: Session, ids: Optional[List[int]]) -> List[Dict[str, Any]]:
    from services.assistants.skill_service import SkillService
    from services.assistants.skill_file_service import SkillFileService
    svc = SkillService(db)
    files_svc = SkillFileService(db)
    rows = svc.get_all(limit=1000)
    out = []
    for sk in rows:
        if ids and sk.id not in ids:
            continue
        tool_refs = []
        for t in (svc.get_tools_for_skill(sk.id) or []):
            server = getattr(t, "mcp_server", None)
            slug = getattr(server, "slug", None)
            remote = getattr(t, "remote_name", None)
            if slug and remote:
                tool_refs.append({"mcp_server_slug": slug, "remote_name": remote})
        files = []
        try:
            for f in files_svc.list_skill_files(sk.id):
                detail = files_svc.get_skill_file(sk.id, f["id"], include_content=True)
                files.append({
                    "relative_path": detail.get("relative_path"),
                    "content": detail.get("content") or "",
                    "content_type": detail.get("content_type"),
                    "is_editable": detail.get("is_editable", True),
                    "is_executable": detail.get("is_executable", False),
                })
        except Exception as exc:  # noqa: BLE001 — files are best-effort
            log.warningx("Skill-bestanden exporteren mislukt", skill_id=sk.id, error=str(exc))
        out.append({
            "name": sk.name,
            "display_name": sk.display_name,
            "description": sk.description or "",
            "instructions": sk.instructions or "",
            "input_schema": sk.input_schema,
            "output_schema": sk.output_schema,
            "is_system": bool(sk.is_system),
            "is_runtime": bool(sk.is_runtime),
            "is_enabled": bool(sk.is_enabled),
            "priority": sk.priority,
            "routing_tags": sk.routing_tags or [],
            "tool_refs": tool_refs,
            "files": files,
        })
    return out


_EXPORTERS = {
    "meeting_profile": _export_meeting_profiles,
    "workflow": _export_workflows,
    "mcp_server": _export_mcp_servers,
    "skill": _export_skills,
}


# ── Import ──────────────────────────────────────────────────────────────────

def import_envelope(db: Session, envelope: Dict[str, Any], user=None) -> Dict[str, Any]:
    if not isinstance(envelope, dict) or "items" not in envelope:
        raise ImportExportError("Not a valid ND3X export (missing 'items').")
    kind = envelope.get("kind")
    if kind not in KINDS:
        raise ImportExportError(f"Unknown or missing export kind '{kind}'.")
    items = envelope.get("items") or []
    if not isinstance(items, list):
        raise ImportExportError("'items' must be a list.")
    results = _IMPORTERS[kind](db, items, user)
    created = sum(1 for r in results if r["status"] in ("created", "renamed"))
    return {"kind": kind, "created": created, "total": len(results), "results": results}


def _unique_name(existing: set[str], name: str) -> str:
    if name not in existing:
        return name
    base = f"{name} (imported)"
    if base not in existing:
        return base
    i = 2
    while f"{name} (imported {i})" in existing:
        i += 1
    return f"{name} (imported {i})"


def _import_meeting_profiles(db: Session, items: List[Dict[str, Any]], user) -> List[Dict[str, Any]]:
    from services.voice.meeting_profile_service import MeetingProfileService
    from schemas.meeting_profile import MeetingProfileCreate
    svc = MeetingProfileService(db)
    existing = {p.name for p in svc.list()}
    results = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            results.append({"name": "(unnamed)", "status": "error", "detail": "missing name"})
            continue
        final = _unique_name(existing, name)
        try:
            created = svc.create(MeetingProfileCreate(
                name=final,
                description=item.get("description"),
                instructions=item.get("instructions"),
                language=item.get("language"),
                output_template=item.get("output_template"),
                action_policy=item.get("action_policy"),
                enabled=bool(item.get("enabled", True)),
                is_default=False,  # never import a default; avoids clobbering
            ))
            existing.add(final)
            results.append({"name": final, "status": "renamed" if final != name else "created", "id": created.id})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "status": "error", "detail": str(exc)[:200]})
    return results


def _import_mcp_servers(db: Session, items: List[Dict[str, Any]], user) -> List[Dict[str, Any]]:
    from services.mcp.mcp_server_service import MCPServerService
    from schemas.mcp_server import MCPServerCreate
    svc = MCPServerService(db)
    rows = svc.get_all(limit=1000)
    names = {s.name for s in rows}
    slugs = {s.slug for s in rows}
    results = []
    for item in items:
        name = (item.get("name") or "").strip()
        slug = (item.get("slug") or "").strip()
        if not name or not slug:
            results.append({"name": name or "(unnamed)", "status": "error", "detail": "missing name/slug"})
            continue
        final_name = _unique_name(names, name)
        final_slug = _unique_name(slugs, slug).replace(" ", "-").replace("(", "").replace(")", "")
        try:
            created = svc.create(MCPServerCreate(
                name=final_name,
                slug=final_slug,
                description=item.get("description"),
                server_type=item.get("server_type") or "http",
                base_url=item.get("base_url"),
                stdio_command=item.get("stdio_command"),
                stdio_install_command=item.get("stdio_install_command"),
                is_enabled=bool(item.get("is_enabled", True)),
            ))
            names.add(final_name)
            slugs.add(final_slug)
            results.append({
                "name": final_name, "status": "renamed" if final_name != name else "created",
                "id": created.id, "detail": "re-sync tools + set auth after import",
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "status": "error", "detail": str(exc)[:200]})
    return results


def _import_workflows(db: Session, items: List[Dict[str, Any]], user) -> List[Dict[str, Any]]:
    from services.workflows.workflow_service import WorkflowService
    from services.assistants.assistant_service import AssistantService
    from schemas.workflow import WorkflowCreate, WorkflowOperationCreate
    from models.workflow import Workflow
    svc = WorkflowService(db)
    assistants = AssistantService(db)
    # The unique constraint on Workflow.name is column-level, so it counts
    # soft-deleted rows too. get_all() hides those (deleted_at IS NULL), which
    # would let us pick a name the DB then rejects. Seed from ALL rows instead.
    existing = {n for (n,) in db.query(Workflow.name).all()}
    results = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            results.append({"name": "(unnamed)", "status": "error", "detail": "missing name"})
            continue
        final = _unique_name(existing, name)
        ops: List[WorkflowOperationCreate] = []
        unresolved: List[str] = []
        for op in item.get("operations") or []:
            ref_id = 0
            if op.get("operation_type") == "assistant" and op.get("ref_name"):
                a = assistants.get_by_name(op["ref_name"])
                if a is None:
                    unresolved.append(op["ref_name"])
                    continue
                ref_id = a.id
            ops.append(WorkflowOperationCreate(
                name=op.get("name") or "step",
                operation_type=op.get("operation_type") or "assistant",
                operation_ref_id=ref_id or op.get("operation_ref_id") or 0,
                config=op.get("config") or {},
                depends_on=op.get("depends_on") or [],
                on_success_follow_up=op.get("on_success_follow_up"),
                on_failure_follow_up=op.get("on_failure_follow_up"),
                join_strategy=op.get("join_strategy") or "all",
                timeout_seconds=op.get("timeout_seconds"),
                retry_policy=op.get("retry_policy") or {},
                position=op.get("position") or 100,
            ))
        try:
            created = svc.create(WorkflowCreate(
                name=final,
                description=item.get("description"),
                input_schema=item.get("input_schema") or {},
                schedule_cron=item.get("schedule_cron"),
                is_enabled=bool(item.get("is_enabled", True)),
                operations=ops,
            ))
            existing.add(final)
            detail = None
            if unresolved:
                detail = f"skipped steps for missing assistants: {', '.join(sorted(set(unresolved)))}"
            results.append({
                "name": final, "status": "renamed" if final != name else "created",
                "id": created.id, **({"detail": detail} if detail else {}),
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "status": "error", "detail": str(exc)[:200]})
    return results


def _import_skills(db: Session, items: List[Dict[str, Any]], user) -> List[Dict[str, Any]]:
    from services.assistants.skill_service import SkillService
    from services.assistants.skill_file_service import SkillFileService
    from services.mcp.mcp_server_service import MCPServerService
    from models.tool import Tool
    from schemas.skill import SkillCreate
    svc = SkillService(db)
    files_svc = SkillFileService(db)
    mcp_svc = MCPServerService(db)
    existing = {s.name for s in svc.get_all(limit=1000)}
    # slug → server id, for tool re-linking
    servers = {s.slug: s.id for s in mcp_svc.get_all(limit=1000)}
    results = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            results.append({"name": "(unnamed)", "status": "error", "detail": "missing name"})
            continue
        final = _unique_name(existing, name)
        try:
            created = svc.create(SkillCreate(
                name=final,
                display_name=item.get("display_name"),
                description=item.get("description") or "",
                instructions=item.get("instructions") or "",
                input_schema=item.get("input_schema"),
                output_schema=item.get("output_schema"),
                is_system=bool(item.get("is_system", False)),
                is_runtime=bool(item.get("is_runtime", False)),
                is_enabled=bool(item.get("is_enabled", True)),
                priority=item.get("priority") or 100,
                routing_tags=item.get("routing_tags") or [],
            ), user=user)
            existing.add(final)
            # Files
            for f in item.get("files") or []:
                rel = f.get("relative_path")
                if not rel:
                    continue
                files_svc.create_or_update_skill_file(
                    created.id, rel, f.get("content") or "",
                    {k: f.get(k) for k in ("content_type", "is_editable", "is_executable")},
                )
            # Tool links — only those that resolve in this install
            linked, skipped = 0, 0
            for ref in item.get("tool_refs") or []:
                sid = servers.get(ref.get("mcp_server_slug"))
                if not sid:
                    skipped += 1
                    continue
                tool = db.query(Tool).filter(
                    Tool.mcp_server_id == sid, Tool.remote_name == ref.get("remote_name")
                ).first()
                if tool is None:
                    skipped += 1
                    continue
                try:
                    svc.link_tool_to_skill(skill_id=created.id, tool_id=tool.id, user=user)
                    linked += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
            detail = None
            if skipped:
                detail = f"{linked} tools linked, {skipped} tool link(s) skipped (not present here)"
            results.append({
                "name": final, "status": "renamed" if final != name else "created",
                "id": created.id, **({"detail": detail} if detail else {}),
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "status": "error", "detail": str(exc)[:200]})
    return results


_IMPORTERS = {
    "meeting_profile": _import_meeting_profiles,
    "mcp_server": _import_mcp_servers,
    "workflow": _import_workflows,
    "skill": _import_skills,
}
