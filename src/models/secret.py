"""
models/secret.py

Native secret store for the interface back-end.

Secrets are stored ENCRYPTED at rest (Fernet via utils.crypto) — the same
pattern provider API keys use (models/provider.api_key_encrypted). The plaintext
value is NEVER stored, logged or returned by the API; it is only decrypted
server-side at the moment of use (e.g. injected into a workflow http_request),
so the AI never sees it.

No soft-delete: the unique constraint on ``name`` is column-level and would
otherwise count deleted rows, blocking re-creation of a previously deleted name.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.sql import func

from db.database import Base


class Secret(Base):
    __tablename__ = "secrets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    # Fernet-encrypted value; NULL means a placeholder (metadata only, no value).
    value_encrypted = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    # A placeholder is a secret that is known/expected but whose value has not
    # been provided yet.
    placeholder = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
