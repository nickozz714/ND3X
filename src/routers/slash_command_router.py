"""
routers/slash_command_router.py

Custom chat slash-commands: list feeds the composer autocomplete (any user);
managing commands requires the Expert role, like other workbench config.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from services.authz_service import assert_expert_role
from schemas.slash_command import SlashCommandCreate, SlashCommandRead, SlashCommandUpdate
from services.slash_command_service import SlashCommandService

router = APIRouter(prefix="/slash-commands", tags=["Slash Commands"])


@router.get("", response_model=list[SlashCommandRead])
def list_commands(enabled_only: bool = False, db: Session = Depends(get_db), user=Depends(require_user)):
    return SlashCommandService(db).list(enabled_only=enabled_only)


@router.post("", response_model=SlashCommandRead)
def create_command(data: SlashCommandCreate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        return SlashCommandService(db).create(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{command_id}", response_model=SlashCommandRead)
def update_command(command_id: int, data: SlashCommandUpdate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        out = SlashCommandService(db).update(command_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if out is None:
        raise HTTPException(status_code=404, detail="Slash command not found")
    return out


@router.delete("/{command_id}")
def delete_command(command_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    if not SlashCommandService(db).delete(command_id):
        raise HTTPException(status_code=404, detail="Slash command not found")
    return {"ok": True}
