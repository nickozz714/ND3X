"""User-definable meeting profiles (dynamic).

The voice/meeting pipeline selects a profile by id (`voice_profiles.registry.get_profile`).
Built-in profiles are code; these DB rows let an admin/user create more profiles that
plug into the same flow — overriding the live assistant's instructions, language and
output template — without code. Resolved by the registry as `mp-<id>`.
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from db.database import Base


class MeetingProfile(Base):
    __tablename__ = "meeting_profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    # Overrides the live meeting assistant's instructions (what to capture/emphasise).
    instructions = Column(Text, nullable=True)
    language = Column(String(64), nullable=True)        # e.g. "nl", "en", "auto"
    output_template = Column(Text, nullable=True)       # optional markdown/output guidance
    # Meeting-driven actions (#9) policy: {enabled, allowed_actions[], allowed_tools[],
    # autonomy, triggers[], action_budget, min_confidence, max_per_tick}. Null/absent
    # = actions disabled for this profile (opt-in).
    action_policy = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
