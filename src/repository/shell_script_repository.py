from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.shell_script import ShellScript

log = get_logger(__name__)


class ShellScriptRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, only_enabled: bool = False) -> list[ShellScript]:
        query = self.db.query(ShellScript)
        if only_enabled:
            query = query.filter(ShellScript.is_enabled == True)
        return query.order_by(ShellScript.slug).all()

    def get_by_id(self, id: int) -> Optional[ShellScript]:
        return self.db.query(ShellScript).filter(ShellScript.id == id).first()

    def get_by_slug(self, slug: str) -> Optional[ShellScript]:
        return self.db.query(ShellScript).filter(ShellScript.slug == slug).first()

    def create(self, data) -> ShellScript:
        now = datetime.now(timezone.utc)
        obj = ShellScript(**data.model_dump(exclude_unset=True))
        obj.created_at = now
        obj.updated_at = now
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        log.infox("ShellScript aangemaakt", slug=obj.slug, id=obj.id)
        return obj

    def update(self, id: int, data) -> Optional[ShellScript]:
        obj = self.get_by_id(id)
        if not obj:
            return None
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(obj, key, value)
        obj.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(obj)
        log.infox("ShellScript bijgewerkt", slug=obj.slug, id=obj.id)
        return obj

    def delete(self, id: int) -> bool:
        obj = self.get_by_id(id)
        if not obj:
            return False
        self.db.delete(obj)
        self.db.commit()
        log.infox("ShellScript verwijderd", id=id)
        return True
