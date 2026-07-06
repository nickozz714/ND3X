"""Filesystem bootstrap layer for first-time setup.

Config + secrets live UNDER the chosen base directory, in ``<base>/.nd3x/``:

- ``bootstrap.json`` — base dir + database connection. Its presence is what makes
  the app "configured".
- ``secrets.json`` — ``JWT_SECRET`` + the Fernet keys (``MAIL_SECRET_KEY`` /
  ``SETTINGS_ENCRYPTION_KEY``). Generated once and kept stable, because the Fernet
  key encrypts provider secrets in the DB and the JWT secret signs sessions —
  regenerating either breaks existing data/logins.

The only thing at a fixed location is a tiny pointer at ``$ND3X_HOME/pointer.json``
(default ``~/.nd3x/pointer.json``) that records which base dir is active, so the
app can find the base dir before it has any other config. ``ND3X_BASE_DIR`` in the
environment overrides the pointer (headless/Docker). Everything else (db/, logs/,
files/, ask/, voice/) lives under the base dir.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
import stat
from pathlib import Path
from typing import Any, Optional


def nd3x_home() -> Path:
    return Path(os.getenv("ND3X_HOME", "~/.nd3x")).expanduser()


def _pointer_path() -> Path:
    return nd3x_home() / "pointer.json"


def active_base_dir() -> Optional[str]:
    """The base dir currently in effect: ND3X_BASE_DIR env, else the pointer."""
    env = os.getenv("ND3X_BASE_DIR")
    if env and env.strip():
        return str(Path(env).expanduser())
    pointer = _pointer_path()
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except Exception:
            return None
        base = data.get("base_dir") if isinstance(data, dict) else None
        return str(Path(base).expanduser()) if base else None
    return None


def config_dir(base_dir: Optional[str] = None) -> Optional[Path]:
    base = base_dir or active_base_dir()
    return (Path(base).expanduser() / ".nd3x") if base else None


def _bootstrap_path(base_dir: Optional[str] = None) -> Optional[Path]:
    cd = config_dir(base_dir)
    return (cd / "bootstrap.json") if cd else None


def _secrets_path(base_dir: Optional[str] = None) -> Optional[Path]:
    cd = config_dir(base_dir)
    return (cd / "secrets.json") if cd else None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def load_bootstrap(base_dir: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the bootstrap config, or None when the app is not yet configured."""
    path = _bootstrap_path(base_dir)
    if not path or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("base_dir"):
        return None
    return data


def is_configured() -> bool:
    return load_bootstrap() is not None


def write_bootstrap(*, base_dir: str, database: dict[str, Any]) -> dict[str, Any]:
    base = str(Path(base_dir).expanduser())
    # Fixed-location pointer so the app can find the base dir next boot.
    _atomic_write(_pointer_path(), json.dumps({"base_dir": base}, indent=2))
    # The real config lives under the base dir.
    data = {"base_dir": base, "database": dict(database or {})}
    _atomic_write(_bootstrap_path(base), json.dumps(data, indent=2))
    return data


def clear_bootstrap() -> None:
    """Remove the pointer so the app re-enters first-time setup. Leaves the
    base-dir config in place (re-point to adopt it again)."""
    try:
        _pointer_path().unlink()
    except FileNotFoundError:
        pass


def load_secrets(base_dir: Optional[str] = None) -> dict[str, str]:
    path = _secrets_path(base_dir)
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def has_secrets(base_dir: Optional[str] = None) -> bool:
    """True when the encryption + JWT secrets are already present for this base."""
    secrets = load_secrets(base_dir)
    return bool(secrets.get("MAIL_SECRET_KEY") and secrets.get("JWT_SECRET"))


def write_secrets(partial: dict[str, str], *, base_dir: Optional[str] = None) -> dict[str, str]:
    """Merge the provided (non-empty) secrets into secrets.json, chmod 600.
    Used when adopting a DB whose original env secrets must be restored."""
    existing = load_secrets(base_dir)
    for key in ("JWT_SECRET", "MAIL_SECRET_KEY", "SETTINGS_ENCRYPTION_KEY"):
        val = (partial or {}).get(key)
        if val and str(val).strip():
            existing[key] = str(val).strip()
    path = _secrets_path(base_dir)
    if path is None:
        raise RuntimeError("Cannot persist secrets without a base directory.")
    _atomic_write(path, json.dumps(existing, indent=2))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return existing


def load_or_create_secrets(*, generate: bool = False, base_dir: Optional[str] = None) -> dict[str, str]:
    """Load persisted secrets. When ``generate`` is set, any missing secret is
    created and the file persisted (chmod 600). Existing values are never
    overwritten — that stability is the whole point."""
    existing = load_secrets(base_dir)
    if not generate:
        return existing

    from cryptography.fernet import Fernet

    changed = False
    if not existing.get("JWT_SECRET"):
        existing["JWT_SECRET"] = _secrets.token_hex(32)
        changed = True
    if not existing.get("MAIL_SECRET_KEY"):
        existing["MAIL_SECRET_KEY"] = Fernet.generate_key().decode("ascii")
        changed = True
    if not existing.get("SETTINGS_ENCRYPTION_KEY"):
        existing["SETTINGS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        changed = True

    if changed:
        path = _secrets_path(base_dir)
        if path is None:
            raise RuntimeError("Cannot persist secrets without a base directory.")
        _atomic_write(path, json.dumps(existing, indent=2))
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
    return existing


def resolve_roots(base_dir: str) -> dict[str, str]:
    """Map the BASE directory to every filesystem root the app needs."""
    base = Path(base_dir).expanduser()
    return {
        "base_dir": str(base),
        "db_path": str(base / "db" / "ND3X.db"),
        "log_file": str(base / "logs" / "app.log"),
        "files_dir": str(base / "files"),
        "ask_root": str(base / "ask"),
        "voice_root": str(base / "voice"),
    }


def ensure_roots(base_dir: str) -> dict[str, str]:
    """Create the filesystem roots under the base dir (db/, logs/, files/, ask/, voice/
    and the .nd3x config dir) so first-time setup can't fail with 'No such file or
    directory' when the base dir (e.g. a fresh Docker volume) is empty. Idempotent."""
    roots = resolve_roots(base_dir)
    base = Path(base_dir).expanduser()
    for d in (
        Path(roots["db_path"]).parent,
        Path(roots["log_file"]).parent,
        Path(roots["files_dir"]),
        Path(roots["ask_root"]),
        Path(roots["voice_root"]),
        base / ".nd3x",
    ):
        d.mkdir(parents=True, exist_ok=True)
    return roots
