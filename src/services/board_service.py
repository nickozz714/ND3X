"""
services/board_service.py

CRUD + selection for the agent's Kanban board. Used by three callers with the
same logic: the board__* builtin tools (agent), the board router (user GUI),
and the board_pull workflow operation (top-N selection for fan-out).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.board import (
    BOARD_ORIGINS,
    BOARD_PRIORITIES,
    BOARD_STATUSES,
    PRIORITY_ORDER,
    BoardItem,
)
from schemas.board import BoardItemCreate, BoardItemUpdate

log = get_logger(__name__)


def _validate_enum(value: Optional[str], allowed: tuple, field: str) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)} (got {value!r})")
    return v


class BoardService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------ read

    def list_items(
        self,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        ready_only: bool = False,
    ) -> List[BoardItem]:
        """Items, ordered for a column: priority desc, then manual position, then
        oldest first. `ready_only` drops items whose dependencies aren't done."""
        q = self.db.query(BoardItem)
        if status:
            q = q.filter(BoardItem.status == _validate_enum(status, BOARD_STATUSES, "status"))
        items = q.all()
        items.sort(key=lambda it: (
            -PRIORITY_ORDER.get((it.priority or "medium"), 1),
            it.position if it.position is not None else 100,
            it.id,
        ))
        if ready_only:
            items = [it for it in items if self.is_ready(it)]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    def get_item(self, item_id: int) -> Optional[BoardItem]:
        return self.db.query(BoardItem).filter(BoardItem.id == item_id).first()

    def is_ready(self, item: BoardItem) -> bool:
        """An item is ready when every dependency is done (or it has none)."""
        deps = list(item.depends_on or [])
        if not deps:
            return True
        done = {
            r.id for r in self.db.query(BoardItem)
            .filter(BoardItem.id.in_(deps), BoardItem.status == "done").all()
        }
        return all(d in done for d in deps)

    def to_dict(self, item: BoardItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "status": item.status,
            "priority": item.priority,
            "acceptance": item.acceptance,
            "depends_on": list(item.depends_on or []),
            "labels": list(item.labels or []),
            "origin": item.origin,
            "updated_by": item.updated_by,
            "result": item.result,
            "position": item.position,
            "workflow_run_id": item.workflow_run_id,
            "thread_id": item.thread_id,
            "ready": self.is_ready(item),
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }

    # ----------------------------------------------------------------- write

    def create_item(self, data: BoardItemCreate, *, actor: str = "user") -> BoardItem:
        actor = _validate_enum(actor, BOARD_ORIGINS, "actor") or "user"
        item = BoardItem(
            title=data.title.strip(),
            description=data.description,
            status=_validate_enum(data.status, BOARD_STATUSES, "status") or "todo",
            priority=_validate_enum(data.priority, BOARD_PRIORITIES, "priority") or "medium",
            acceptance=data.acceptance,
            depends_on=list(data.depends_on or []),
            labels=[str(x) for x in (data.labels or [])],
            origin=_validate_enum(data.origin, BOARD_ORIGINS, "origin") or actor,
            updated_by=actor,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        log.infox("Board item aangemaakt", item_id=item.id, actor=actor, status=item.status)
        return item

    def update_item(self, item_id: int, data: BoardItemUpdate, *, actor: str = "user") -> Optional[BoardItem]:
        item = self.get_item(item_id)
        if item is None:
            return None
        actor = _validate_enum(actor, BOARD_ORIGINS, "actor") or "user"
        fields = data.model_dump(exclude_unset=True)
        if "status" in fields:
            fields["status"] = _validate_enum(fields["status"], BOARD_STATUSES, "status")
        if "priority" in fields:
            fields["priority"] = _validate_enum(fields["priority"], BOARD_PRIORITIES, "priority")
        if "labels" in fields and fields["labels"] is not None:
            fields["labels"] = [str(x) for x in fields["labels"]]
        for k, v in fields.items():
            if v is not None or k in ("description", "acceptance", "result"):
                setattr(item, k, v)
        item.updated_by = actor
        self.db.commit()
        self.db.refresh(item)
        log.infox("Board item bijgewerkt", item_id=item.id, actor=actor,
                  changed=list(fields.keys()))
        return item

    def move_item(self, item_id: int, status: str, *, result: Optional[str] = None,
                  actor: str = "user") -> Optional[BoardItem]:
        patch = BoardItemUpdate(status=status)
        if result is not None:
            patch.result = result
        return self.update_item(item_id, patch, actor=actor)

    def delete_item(self, item_id: int) -> bool:
        item = self.get_item(item_id)
        if item is None:
            return False
        self.db.delete(item)
        self.db.commit()
        log.infox("Board item verwijderd", item_id=item_id)
        return True

    # ------------------------------------------------------ workflow support

    def pull(self, *, status: str = "todo", limit: int = 3, ready_only: bool = True) -> List[BoardItem]:
        """The board_pull selection: top-N of a column by priority, dependency-ready.

        This is the 'work the TODO / a subset' primitive — a workflow fans out
        over the returned items via For-Each."""
        return self.list_items(status=status, limit=max(0, int(limit)), ready_only=ready_only)
