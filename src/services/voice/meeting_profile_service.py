"""CRUD for dynamic meeting profiles + resolution into runtime profile objects.

Runtime profile id convention: DB profiles are addressed as ``mp-<id>`` so the
voice registry can tell them apart from code profiles.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from models.meeting_profile import MeetingProfile
from schemas.meeting_profile import (
    MeetingProfileCreate, MeetingProfileUpdate, MeetingProfileRead, AvailableProfile,
)
from services.voice.voice_profiles.db_profile import DbMeetingProfile


def _read(p: MeetingProfile) -> MeetingProfileRead:
    return MeetingProfileRead(
        id=p.id, name=p.name, description=p.description, instructions=p.instructions,
        language=p.language, output_template=p.output_template,
        action_policy=p.action_policy,
        enabled=bool(p.enabled), is_default=bool(p.is_default),
    )


def runtime_id(db_id: int) -> str:
    return f"mp-{db_id}"


class MeetingProfileService:
    def __init__(self, db: Session):
        self.db = db

    def list(self) -> List[MeetingProfileRead]:
        return [_read(p) for p in self.db.query(MeetingProfile).order_by(MeetingProfile.name).all()]

    def create(self, data: MeetingProfileCreate) -> MeetingProfileRead:
        if data.is_default:
            self._clear_default()
        obj = MeetingProfile(**data.model_dump())
        self.db.add(obj); self.db.commit(); self.db.refresh(obj)
        return _read(obj)

    def update(self, profile_id: int, data: MeetingProfileUpdate) -> Optional[MeetingProfileRead]:
        obj = self.db.get(MeetingProfile, profile_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        if fields.get("is_default"):
            self._clear_default()
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.commit(); self.db.refresh(obj)
        return _read(obj)

    def delete(self, profile_id: int) -> bool:
        obj = self.db.get(MeetingProfile, profile_id)
        if obj is None:
            return False
        self.db.delete(obj); self.db.commit()
        return True

    def _clear_default(self) -> None:
        for p in self.db.query(MeetingProfile).filter(MeetingProfile.is_default == True).all():  # noqa: E712
            p.is_default = False

    def available(self) -> List[AvailableProfile]:
        """Code profiles + enabled DB profiles, for the meeting profile picker."""
        from services.voice.voice_profiles.registry import PROFILES
        out = [AvailableProfile(id=pid, name=getattr(p, "name", pid), source="builtin") for pid, p in PROFILES.items()]
        for p in self.db.query(MeetingProfile).filter(MeetingProfile.enabled == True).order_by(MeetingProfile.name).all():  # noqa: E712
            out.append(AvailableProfile(id=runtime_id(p.id), name=p.name, source="custom"))
        return out


def resolve_db_profile(profile_id: str) -> Optional[DbMeetingProfile]:
    """Build a runtime profile for an ``mp-<id>`` id, or None. Own DB session so the
    voice registry stays decoupled."""
    if not profile_id or not profile_id.startswith("mp-"):
        return None
    try:
        db_id = int(profile_id[3:])
    except ValueError:
        return None
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        p = db.get(MeetingProfile, db_id)
        if p is None or not p.enabled:
            return None
        return DbMeetingProfile(id=profile_id, name=p.name, instructions=p.instructions,
                                language=p.language, output_template=p.output_template)
    finally:
        db.close()


def default_runtime_profile_id() -> Optional[str]:
    """The runtime id of the DB profile marked default, if any."""
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        p = db.query(MeetingProfile).filter(MeetingProfile.is_default == True, MeetingProfile.enabled == True).first()  # noqa: E712
        return runtime_id(p.id) if p else None
    finally:
        db.close()
