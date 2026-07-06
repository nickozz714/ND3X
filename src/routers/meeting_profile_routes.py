from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from schemas.meeting_profile import (
    MeetingProfileCreate, MeetingProfileUpdate, MeetingProfileRead, AvailableProfile,
    MeetingProfileTemplate,
)
from services.voice.meeting_profile_service import MeetingProfileService
from services.voice.meeting_profile_templates import MEETING_PROFILE_TEMPLATES

router = APIRouter(prefix="/meeting-profiles", tags=["meeting-profiles"], dependencies=[Depends(require_user)])


def _svc(db: Session = Depends(get_db)) -> MeetingProfileService:
    return MeetingProfileService(db)


@router.get("/available", response_model=list[AvailableProfile])
def available_profiles(svc: MeetingProfileService = Depends(_svc)):
    """Selectable profiles (built-in + custom) for the meeting profile picker."""
    return svc.available()


@router.get("/templates", response_model=list[MeetingProfileTemplate])
def list_templates():
    """Starter templates the user can create a profile from (guidance)."""
    return MEETING_PROFILE_TEMPLATES


@router.post("/generate")
async def generate_profile_with_ai(body: dict, db: Session = Depends(get_db)):
    """Generate a draft meeting profile from wizard answers using the AI model on
    the cognition/planner slot. Returns a draft the user can review + save."""
    from services.voice.meeting_profile_ai import generate_profile
    try:
        return await generate_profile(db, body or {})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=list[MeetingProfileRead])
def list_profiles(svc: MeetingProfileService = Depends(_svc)):
    return svc.list()


@router.post("", response_model=MeetingProfileRead, status_code=201)
def create_profile(body: MeetingProfileCreate, svc: MeetingProfileService = Depends(_svc)):
    return svc.create(body)


@router.put("/{profile_id}", response_model=MeetingProfileRead)
def update_profile(profile_id: int, body: MeetingProfileUpdate, svc: MeetingProfileService = Depends(_svc)):
    out = svc.update(profile_id, body)
    if out is None:
        raise HTTPException(404, "Meeting profile not found")
    return out


@router.delete("/{profile_id}")
def delete_profile(profile_id: int, svc: MeetingProfileService = Depends(_svc)):
    if not svc.delete(profile_id):
        raise HTTPException(404, "Meeting profile not found")
    return {"deleted": True}
