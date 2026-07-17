"""
services/builtin/tools/board_tools.py

The agent's own Kanban board, exposed as always-on builtin tools. This is the
agent's working memory of tasks — it lists what needs doing, creates/updates
items, and moves them across the fixed columns (todo → doing → blocked → done).
The user manages the same board from the board GUI tile.

Registered on import (see ask_job_callbacks). Engine-agnostic: the orchestrator
calls these directly; the Claude Code engine reaches them via MCP.
"""
from __future__ import annotations

from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


def _svc(db):
    from services.board_service import BoardService
    return BoardService(db)


@internal_tool_registry.register(
    name="board__list",
    title="List Board Items",
    description=(
        "List items on your Kanban board. Optionally filter by column status "
        "(todo | doing | blocked | done) and cap the count. Items come back "
        "ordered by priority (urgent first) then board position. Set "
        "ready_only=true to see only items whose dependencies are all done — "
        "the ones you can actually start now. Use this to decide what to work on."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["todo", "doing", "blocked", "done"],
                       "description": "Only items in this column."},
            "limit": {"type": "integer", "description": "Max items to return."},
            "ready_only": {"type": "boolean",
                           "description": "Only items with all dependencies done."},
        },
    },
    tags=["internal", "board"],
)
async def board_list(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    with SessionLocal() as db:
        svc = _svc(db)
        try:
            items = svc.list_items(
                status=a.get("status"),
                limit=a.get("limit"),
                ready_only=bool(a.get("ready_only")),
            )
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "count": len(items),
                "items": [svc.to_dict(i) for i in items]}


@internal_tool_registry.register(
    name="board__get",
    title="Get Board Item",
    description="Get one board item by id, including its description, acceptance criteria, dependencies and result.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "Board item id."}},
        "required": ["id"],
    },
    tags=["internal", "board"],
)
async def board_get(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    item_id = (args or {}).get("id")
    with SessionLocal() as db:
        svc = _svc(db)
        item = svc.get_item(int(item_id)) if item_id is not None else None
        if item is None:
            return {"status": "error", "error": f"No board item with id={item_id}."}
        return {"status": "success", "item": svc.to_dict(item)}


@internal_tool_registry.register(
    name="board__create",
    title="Create Board Item",
    description=(
        "Add a new item to your board. Give it a clear title, and where useful a "
        "description, acceptance criteria ('done when …'), priority "
        "(low | medium | high | urgent), labels, and depends_on (ids of items "
        "that must finish first — an item with unfinished dependencies belongs in "
        "'blocked'). New items default to the 'todo' column."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["todo", "doing", "blocked", "done"]},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
            "acceptance": {"type": "string", "description": "Done-when criteria."},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title"],
    },
    tags=["internal", "board"],
)
async def board_create(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from schemas.board import BoardItemCreate
    a = args or {}
    if not str(a.get("title") or "").strip():
        return {"status": "error", "error": "board__create requires a non-empty 'title'."}
    with SessionLocal() as db:
        svc = _svc(db)
        try:
            data = BoardItemCreate(
                title=a["title"],
                description=a.get("description"),
                status=a.get("status") or "todo",
                priority=a.get("priority") or "medium",
                acceptance=a.get("acceptance"),
                depends_on=[int(x) for x in (a.get("depends_on") or [])],
                labels=[str(x) for x in (a.get("labels") or [])],
                origin="agent",
            )
            item = svc.create_item(data, actor="agent")
        except (ValueError, TypeError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "success", "item": svc.to_dict(item)}


@internal_tool_registry.register(
    name="board__update",
    title="Update Board Item",
    description=(
        "Update fields of a board item: title, description, status "
        "(todo | doing | blocked | done), priority, acceptance, depends_on, "
        "labels, and result. Set 'result' with a short summary of the outcome "
        "when you finish an item, and move it to 'done'. Only the fields you "
        "pass are changed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["todo", "doing", "blocked", "done"]},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
            "acceptance": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
            "labels": {"type": "array", "items": {"type": "string"}},
            "result": {"type": "string", "description": "Outcome summary (set when done)."},
        },
        "required": ["id"],
    },
    tags=["internal", "board"],
)
async def board_update(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from schemas.board import BoardItemUpdate
    a = dict(args or {})
    item_id = a.pop("id", None)
    if item_id is None:
        return {"status": "error", "error": "board__update requires 'id'."}
    with SessionLocal() as db:
        svc = _svc(db)
        try:
            patch = BoardItemUpdate(**{k: v for k, v in a.items()
                                       if k in BoardItemUpdate.model_fields})
            item = svc.update_item(int(item_id), patch, actor="agent")
        except (ValueError, TypeError) as exc:
            return {"status": "error", "error": str(exc)}
        if item is None:
            return {"status": "error", "error": f"No board item with id={item_id}."}
        return {"status": "success", "item": svc.to_dict(item)}


@internal_tool_registry.register(
    name="board__move",
    title="Move Board Item",
    description=(
        "Shorthand to move an item to another column (todo | doing | blocked | "
        "done), optionally recording a result. Equivalent to board__update with "
        "just status (+ result)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": ["todo", "doing", "blocked", "done"]},
            "result": {"type": "string"},
        },
        "required": ["id", "status"],
    },
    tags=["internal", "board"],
)
async def board_move(args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    a = args or {}
    item_id, status = a.get("id"), a.get("status")
    if item_id is None or not status:
        return {"status": "error", "error": "board__move requires 'id' and 'status'."}
    with SessionLocal() as db:
        svc = _svc(db)
        try:
            item = svc.move_item(int(item_id), str(status), result=a.get("result"), actor="agent")
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        if item is None:
            return {"status": "error", "error": f"No board item with id={item_id}."}
        return {"status": "success", "item": svc.to_dict(item)}
