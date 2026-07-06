"""
models/slash_command.py

Custom chat slash-commands (Claude-Code-style): a /name the user types in the
chat composer that expands to a reusable prompt template before submit. The
template may contain a `{args}` placeholder for everything typed after the
command; @Token prompt-variables inside the template keep working (they are
resolved later by the normal pipeline).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from db.database import Base


class SlashCommand(Base):
    __tablename__ = "slash_commands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True, index=True)  # e.g. "review"
    description = Column(String(255), nullable=False, default="")
    template = Column(Text, nullable=False)  # may contain {args}
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
