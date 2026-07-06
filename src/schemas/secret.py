"""schemas/secret.py — DTOs for the native secret store."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SecretCreate(BaseModel):
    name: str
    value: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    placeholder: bool = False


class SecretUpdate(BaseModel):
    value: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    placeholder: bool | None = None


class SecretMetadata(BaseModel):
    """Never carries the plaintext value — only whether one is set."""

    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    placeholder: bool = False
    has_value: bool = False
    created_at: datetime
    updated_at: datetime


class SecretValueObfuscated(BaseModel):
    name: str
    value_obfuscated: str
    has_value: bool = True


class DeleteResponse(BaseModel):
    ok: bool
    deleted: str


class ImportEnvRequest(BaseModel):
    content: str
    overwrite: bool = False


class ImportEnvResult(BaseModel):
    created: int
    updated: int
    skipped: int
    total: int
    names: list[str] = Field(default_factory=list)
