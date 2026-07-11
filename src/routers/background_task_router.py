"""
routers/background_task_router.py

Takenpaneel voor achtergrondtaken (task__create-subagents): lijst + resultaat
inzien voor elke ingelogde gebruiker; annuleren/opschonen is Expert-gated,
zoals ander workbench-beheer. Leest uit de persistente background_tasks-tabel
(de spiegel van de in-memory registry, dus ook historie van vóór een herstart).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from schemas.background_task import BackgroundTaskRead
from services.authz_service import assert_expert_role
from services.background_task_service import BackgroundTaskService

router = APIRouter(prefix="/background-tasks", tags=["Background Tasks"])


@router.get("", response_model=list[BackgroundTaskRead])
def list_tasks(thread_id: str | None = None, limit: int = 200, db: Session = Depends(get_db), user=Depends(require_user)):
    return BackgroundTaskService(db).list(thread_id=thread_id, limit=limit)


@router.get("/{task_id}", response_model=BackgroundTaskRead)
def get_task(task_id: str, db: Session = Depends(get_db), user=Depends(require_user)):
    obj = BackgroundTaskService(db).get(task_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Background task not found")
    return obj


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    from services.builtin.tools import background_tasks as bt

    obj = BackgroundTaskService(db).get(task_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Background task not found")
    cancelled = await bt.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="Task is not running (already finished or lost after a restart)")
    return {"ok": True}


@router.delete("/{task_id}")
async def delete_task(task_id: str, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    from services.builtin.tools import background_tasks as bt

    if bt.is_task_running(task_id):
        raise HTTPException(status_code=409, detail="Task is still running; cancel first, then delete")
    async with bt._TASKS_LOCK:
        bt._TASKS.pop(task_id, None)
    if not BackgroundTaskService(db).delete(task_id):
        raise HTTPException(status_code=404, detail="Background task not found")
    return {"ok": True}
