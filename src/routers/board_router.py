"""
routers/board_router.py

User-facing CRUD for the agent's Kanban board (the board GUI tile). The agent
mutates the same board through the board__* builtin tools; this router is the
human side — list, create, edit, move and delete items.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from schemas.board import BoardItemCreate, BoardItemRead, BoardItemUpdate
from services.board_service import BoardService

router = APIRouter(prefix="/board", tags=["Board"])


def _svc(db: Session) -> BoardService:
    return BoardService(db)


@router.get("/items", response_model=List[BoardItemRead])
def list_items(
    status: Optional[str] = Query(None),
    ready_only: bool = Query(False),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    svc = _svc(db)
    try:
        items = svc.list_items(status=status, ready_only=ready_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Attach the computed `ready` flag (not a column) per item.
    return [BoardItemRead(**svc.to_dict(i)) for i in items]


@router.post("/items", response_model=BoardItemRead)
def create_item(data: BoardItemCreate, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    try:
        item = svc.create_item(data, actor="user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return BoardItemRead(**svc.to_dict(item))


@router.put("/items/{item_id}", response_model=BoardItemRead)
def update_item(item_id: int, data: BoardItemUpdate, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = _svc(db)
    try:
        item = svc.update_item(item_id, data, actor="user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="Board item not found")
    return BoardItemRead(**svc.to_dict(item))


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    if not _svc(db).delete_item(item_id):
        raise HTTPException(status_code=404, detail="Board item not found")
    return {"ok": True}
