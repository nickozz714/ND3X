"""
services/secret_service.py

Business logic for the native, encrypted secret store.

Guarantees:
- Values are Fernet-encrypted at rest (utils.crypto) and only ever decrypted
  server-side, at the moment of use.
- The API/service never returns plaintext: list/get expose metadata only; the
  "value" endpoint returns an obfuscated form; ``get_value`` (raw decrypt) is for
  internal callers (e.g. the workflow executor) and must never feed the LLM.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.secret import Secret
from schemas.secret import SecretCreate, SecretUpdate
from utils.crypto import decrypt_value, encrypt_value

log = get_logger(__name__)

# ${secret.NAME} or ${secrets.NAME}; names allow the same charset the FE validates.
_PLACEHOLDER_RE = re.compile(r"\$\{\s*secrets?\.([A-Za-z0-9_.\-/]+)\s*\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class SecretError(ValueError):
    pass


class SecretService:
    def __init__(self, db: Session):
        self.db = db

    # ── queries ───────────────────────────────────────────────────────────
    def list(self) -> list[Secret]:
        return self.db.query(Secret).order_by(Secret.name.asc()).all()

    def get(self, name: str) -> Optional[Secret]:
        return self.db.query(Secret).filter(Secret.name == name).first()

    def _require(self, name: str) -> Secret:
        row = self.get(name)
        if row is None:
            raise SecretError(f"Secret '{name}' not found")
        return row

    # ── mutations ─────────────────────────────────────────────────────────
    def create(self, data: SecretCreate) -> Secret:
        name = (data.name or "").strip()
        if not name:
            raise SecretError("Secret name is required")
        if not _NAME_RE.match(name):
            raise SecretError("Secret name may only contain letters, digits and _ . - /")
        if self.get(name) is not None:
            raise SecretError(f"Secret '{name}' already exists")
        row = Secret(
            name=name,
            value_encrypted=encrypt_value(data.value) if data.value else None,
            description=data.description,
            tags=list(data.tags or []),
            placeholder=bool(data.placeholder) or not data.value,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        log.infox("Secret aangemaakt", name=name, has_value=row.value_encrypted is not None)
        return row

    def update(self, name: str, data: SecretUpdate) -> Secret:
        row = self._require(name)
        if data.value is not None:
            # Empty string clears the value (back to placeholder); non-empty sets it.
            row.value_encrypted = encrypt_value(data.value) if data.value else None
        if data.description is not None:
            row.description = data.description
        if data.tags is not None:
            row.tags = list(data.tags)
        if data.placeholder is not None:
            row.placeholder = bool(data.placeholder)
        self.db.commit()
        self.db.refresh(row)
        log.infox("Secret bijgewerkt", name=name, has_value=row.value_encrypted is not None)
        return row

    def delete(self, name: str) -> None:
        row = self._require(name)
        self.db.delete(row)
        self.db.commit()
        log.infox("Secret verwijderd", name=name)

    # ── value access ──────────────────────────────────────────────────────
    def get_value(self, name: str) -> Optional[str]:
        """Raw decrypted value — INTERNAL callers only. Never return to the AI."""
        row = self.get(name)
        if row is None or not row.value_encrypted:
            return None
        return decrypt_value(row.value_encrypted)

    def get_value_obfuscated(self, name: str) -> str:
        value = self.get_value(name)
        return self._obfuscate(value)

    @staticmethod
    def _obfuscate(value: Optional[str]) -> str:
        if not value:
            return ""
        n = len(value)
        if n <= 4:
            return "•" * n
        return f"{value[:2]}{'•' * min(n - 4, 12)}{value[-2:]}"

    # ── .env import ───────────────────────────────────────────────────────
    def import_env(self, content: str, overwrite: bool = False) -> dict:
        """Parse a .env-style blob (KEY=VALUE per line) into individual secrets."""
        created = updated = skipped = 0
        names: list[str] = []
        for key, value in self._parse_env(content):
            names.append(key)
            existing = self.get(key)
            if existing is None:
                self.create(SecretCreate(name=key, value=value, tags=["env"]))
                created += 1
            elif overwrite:
                self.update(key, SecretUpdate(value=value))
                updated += 1
            else:
                skipped += 1
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "total": len(names),
            "names": names,
        }

    @staticmethod
    def _parse_env(content: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for raw in (content or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or not _NAME_RE.match(key):
                continue
            value = value.strip()
            # Strip matching surrounding quotes.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            out.append((key, value))
        return out

    # ── placeholder resolution ────────────────────────────────────────────
    def resolve_placeholders(self, text: str) -> tuple[str, list[str], list[str]]:
        """Replace ${secret.NAME} / ${secrets.NAME} with decrypted values.

        Returns (resolved_text, resolved_values, unresolved_names). The raw
        resolved values are returned so the caller can mask them out of any
        trace/log/output that could reach the AI.
        """
        resolved_values: list[str] = []
        unresolved: list[str] = []

        def _sub(match: re.Match) -> str:
            name = match.group(1)
            value = self.get_value(name)
            if value is None:
                unresolved.append(name)
                return match.group(0)
            resolved_values.append(value)
            return value

        return _PLACEHOLDER_RE.sub(_sub, text), resolved_values, unresolved

    @staticmethod
    def has_placeholder(text: str) -> bool:
        return bool(_PLACEHOLDER_RE.search(text or ""))
