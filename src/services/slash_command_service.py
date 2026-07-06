"""
services/slash_command_service.py

CRUD for custom chat slash-commands. Builtin commands (/plan, /model, ...)
live in the front-end (they toggle composer state); this service only manages
the user-defined template commands the composer expands before submit.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.slash_command import SlashCommand
from schemas.slash_command import SlashCommandCreate, SlashCommandUpdate

log = get_logger(__name__)


class SlashCommandService:
    def __init__(self, db: Session):
        self.db = db

    def list(self, *, enabled_only: bool = False) -> List[SlashCommand]:
        q = self.db.query(SlashCommand)
        if enabled_only:
            q = q.filter(SlashCommand.is_enabled.is_(True))
        return q.order_by(SlashCommand.name).all()

    def get(self, command_id: int) -> Optional[SlashCommand]:
        return self.db.query(SlashCommand).filter(SlashCommand.id == command_id).first()

    def create(self, data: SlashCommandCreate) -> SlashCommand:
        existing = self.db.query(SlashCommand).filter(SlashCommand.name == data.name).first()
        if existing is not None:
            raise ValueError(f"A command named '/{data.name}' already exists.")
        obj = SlashCommand(
            name=data.name,
            description=data.description or "",
            template=data.template,
            is_enabled=bool(data.is_enabled),
        )
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        log.infox("Slash command aangemaakt", name=obj.name, command_id=obj.id)
        return obj

    def update(self, command_id: int, data: SlashCommandUpdate) -> Optional[SlashCommand]:
        obj = self.get(command_id)
        if obj is None:
            return None
        if data.name is not None and data.name != obj.name:
            clash = self.db.query(SlashCommand).filter(SlashCommand.name == data.name).first()
            if clash is not None:
                raise ValueError(f"A command named '/{data.name}' already exists.")
            obj.name = data.name
        if data.description is not None:
            obj.description = data.description
        if data.template is not None:
            obj.template = data.template
        if data.is_enabled is not None:
            obj.is_enabled = bool(data.is_enabled)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, command_id: int) -> bool:
        obj = self.get(command_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.commit()
        log.infox("Slash command verwijderd", name=obj.name, command_id=command_id)
        return True
