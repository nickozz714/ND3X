# services/voice_profiles/registry.py
from __future__ import annotations
from typing import Dict
from services.voice.voice_profiles.base import VoiceLiveProfile
from services.voice.voice_profiles.default_meeting import default_meeting_profile
from services.voice.voice_profiles.requirements_engineering import requirements_engineering_profile

PROFILES: Dict[str, VoiceLiveProfile] = {
    default_meeting_profile.id: default_meeting_profile,
    requirements_engineering_profile.id: requirements_engineering_profile,
}

def get_profile(profile_id: str | None) -> VoiceLiveProfile:
    if not profile_id:
        return default_meeting_profile
    if profile_id in PROFILES:
        return PROFILES[profile_id]
    # DB-defined profile (mp-<id>) — resolved lazily so this registry stays DB-decoupled.
    try:
        from services.voice.meeting_profile_service import resolve_db_profile
        db_profile = resolve_db_profile(profile_id)
        if db_profile is not None:
            return db_profile  # duck-typed VoiceLiveProfile
    except Exception:  # noqa: BLE001 — never break meeting start on profile lookup
        pass
    return default_meeting_profile
