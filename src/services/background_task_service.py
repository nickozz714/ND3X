"""
services/background_task_service.py

Leest/beheert de persistente achtergrondtaken-tabel (de spiegel van de
in-memory registry in services/builtin/tools/background_tasks.py). Gebruikt
door het takenpaneel in de workbench.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models.background_task import BackgroundTask


class BackgroundTaskService:
    def __init__(self, db: Session):
        self.db = db

    def list(self, thread_id: Optional[str] = None, limit: int = 200) -> list[BackgroundTask]:
        q = self.db.query(BackgroundTask)
        if thread_id:
            q = q.filter(BackgroundTask.owner_thread == thread_id)
        return q.order_by(BackgroundTask.created_at.desc()).limit(max(1, min(limit, 1000))).all()

    def get(self, task_id: str) -> Optional[BackgroundTask]:
        return self.db.query(BackgroundTask).filter(BackgroundTask.id == task_id).first()

    def delete(self, task_id: str) -> bool:
        obj = self.get(task_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True
