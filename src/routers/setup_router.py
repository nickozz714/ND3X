"""First-time setup — unauthenticated, self-locking.

Active only until the app is set up (a configured DB with an admin user). Once an
admin exists, `initialize` returns 409 and the wizard disappears. This replaces
the removed ND3X_BOOTSTRAP_* environment path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from component import runtime_paths
from component.config import settings
from component.logging import get_logger

router = APIRouter(prefix="/setup", tags=["setup"])
log = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────────
class DbConfig(BaseModel):
    dialect: str = "sqlite"  # "sqlite" | "mysql"
    sqlite_path: Optional[str] = None  # blank => <BASE>/db/nd3x.db
    host: Optional[str] = None
    port: Optional[int] = 3306
    user: Optional[str] = None
    password: Optional[str] = None
    name: Optional[str] = None


class SetupModel(BaseModel):
    model_id: str
    capability: str  # chat | embeddings | transcription | tts | realtime ...
    display_name: Optional[str] = None


class SetupProvider(BaseModel):
    name: str
    provider_type: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    is_local: bool = False
    models: list[SetupModel] = []


class SetupAssignment(BaseModel):
    slot: str
    model_id: str  # matches one of the created models' model_id


class SetupAdmin(BaseModel):
    email: str
    password: str


class InitializePayload(BaseModel):
    # base_dir present => interactive path (write bootstrap + generate secrets).
    # absent => storage already configured via env; just create the admin etc.
    base_dir: Optional[str] = None
    database: DbConfig = DbConfig()
    admin: Optional[SetupAdmin] = None  # omit when adopting a DB that has an admin
    providers: list[SetupProvider] = []
    assignments: list[SetupAssignment] = []
    settings: dict[str, str] = {}
    # Original env-only secrets, when adopting a DB whose secrets.json is missing
    # (keys: JWT_SECRET, MAIL_SECRET_KEY, SETTINGS_ENCRYPTION_KEY). Blanks are
    # generated instead.
    secrets: dict[str, str] = {}


class ProbeDbPayload(BaseModel):
    base_dir: Optional[str] = None
    database: DbConfig = DbConfig()


class BrowsePayload(BaseModel):
    path: Optional[str] = None


class DiscoverModelsPayload(BaseModel):
    provider_type: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────
def _admin_exists() -> bool:
    """True if a usable DB has at least one user. False (not an error) when the
    DB isn't reachable yet — that just means setup hasn't run."""
    if not settings.CONFIGURED:
        return False
    try:
        from db.database import get_session_factory
        db = get_session_factory()()
        try:
            from models.authenticate import User  # noqa: WPS433
            return db.query(User).count() > 0
        finally:
            db.close()
    except Exception:
        return False


def _resolve_sqlite_path(base_dir: Optional[str], db: DbConfig) -> str:
    if db.sqlite_path and db.sqlite_path.strip():
        return str(Path(db.sqlite_path).expanduser())
    if base_dir:
        return runtime_paths.resolve_roots(base_dir)["db_path"]
    return "./db/nd3x.db"


def _mysql_url(db: DbConfig) -> str:
    import urllib.parse
    pw = urllib.parse.quote_plus(db.password or "")
    return (
        f"mysql+pymysql://{db.user}:{pw}@{db.host}:{int(db.port or 3306)}/"
        f"{db.name}?charset=utf8mb4"
    )


def _apply_storage_to_settings(base_dir: Optional[str], db: DbConfig) -> dict:
    """Mutate the live settings + return the bootstrap `database` dict."""
    dialect = (db.dialect or "sqlite").strip().lower()
    settings.DB_DIALECT = dialect
    if dialect == "sqlite":
        sqlite_path = _resolve_sqlite_path(base_dir, db)
        settings.SQLITE_PATH = sqlite_path
        settings.SQLITE_URL = f"sqlite:///{Path(sqlite_path).expanduser().resolve()}"
        return {"dialect": "sqlite", "sqlite_path": sqlite_path}
    if dialect == "mysql":
        settings.DB_HOST = db.host or ""
        settings.DB_PORT = int(db.port or 3306)
        settings.DB_USER = db.user or ""
        settings.DB_PASS = db.password or ""
        settings.DB_NAME = db.name or ""
        settings.MYSQL_URL = _mysql_url(db)
        return {
            "dialect": "mysql", "host": db.host, "port": int(db.port or 3306),
            "user": db.user, "pass": db.password, "name": db.name,
        }
    raise HTTPException(400, f"Unsupported database dialect: {db.dialect!r}")


def _inspect_sqlite(path: str) -> dict:
    """Read-only peek at an existing sqlite DB: does it have the schema + an admin?"""
    import json as _json
    import sqlite3

    info = {"exists": False, "has_schema": False, "has_admin": False, "admin_count": 0}
    p = Path(path).expanduser()
    if not p.is_file():
        return info
    info["exists"] = True
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" in tables:
                info["has_schema"] = True
                count = 0
                for (roles,) in con.execute("SELECT roles FROM users"):
                    try:
                        parsed = _json.loads(roles) if roles else []
                    except Exception:
                        parsed = []
                    if any(str(r).strip().lower() == "admin" for r in (parsed or [])):
                        count += 1
                info["admin_count"] = count
                info["has_admin"] = count > 0
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — inspection is best-effort
        pass
    return info


# ── Endpoints ────────────────────────────────────────────────────────────────
@router.get("/status")
def setup_status() -> dict:
    has_admin = _admin_exists()
    return {
        "configured": bool(settings.CONFIGURED),
        "has_admin": has_admin,
        # The wizard shows while the app isn't fully usable yet.
        "setup_complete": bool(settings.CONFIGURED) and has_admin,
        "nd3x_home": str(runtime_paths.nd3x_home()),
        "base_dir": settings.BASE_DIR or None,
    }


@router.post("/browse")
def browse(payload: BrowsePayload) -> dict:
    """List directories + database files on the server, for the wizard's pickers.
    Only available before setup completes."""
    if _admin_exists():
        raise HTTPException(403, "Setup already completed.")
    raw = (payload.path or "").strip() or str(Path.home())
    try:
        base = Path(raw).expanduser().resolve()
    except Exception:
        base = Path.home()
    if not base.is_dir():
        raise HTTPException(400, f"Not a directory: {base}")
    dirs: list[str] = []
    db_files: list[str] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
                    db_files.append(entry.name)
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {base}")
    return {
        "path": str(base),
        "parent": str(base.parent),
        "home": str(Path.home()),
        "dirs": dirs,
        "db_files": db_files,
    }


@router.post("/discover-models")
def discover_setup_models(payload: DiscoverModelsPayload) -> dict:
    """Discover the models a provider exposes from credentials entered in the wizard
    (no saved provider needed), so the user doesn't have to hand-type model ids.
    Only available before setup completes."""
    if _admin_exists():
        raise HTTPException(403, "Setup already completed.")
    from services.providers.model_discovery import discover_models
    return discover_models(
        provider_type=payload.provider_type,
        base_url=(payload.base_url or None),
        api_key=(payload.api_key or None),
    )


@router.post("/probe-db")
def probe_db(payload: ProbeDbPayload) -> dict:
    """Validate a proposed DB config without committing anything. For an existing
    sqlite file, also report whether it already has the schema + an admin."""
    db = payload.database
    dialect = (db.dialect or "sqlite").strip().lower()
    try:
        if dialect == "sqlite":
            sqlite_path = _resolve_sqlite_path(payload.base_dir, db)
            info = _inspect_sqlite(sqlite_path)
            secrets_present = runtime_paths.has_secrets(payload.base_dir)
            if not info["exists"]:
                parent = Path(sqlite_path).expanduser().resolve().parent
                parent.mkdir(parents=True, exist_ok=True)
                if not os.access(parent, os.W_OK):
                    return {"ok": False, "error": f"Directory not writable: {parent}"}
                return {"ok": True, "detail": f"New database will be created: {sqlite_path}",
                        "has_secrets": secrets_present, **info}
            if info["has_admin"]:
                detail = ("Existing database — already set up; you can adopt it."
                          if secrets_present else
                          "Existing database — already set up, but its secrets are missing "
                          "(provide the original keys to read saved provider keys).")
            else:
                detail = "Existing database found."
            return {"ok": True, "detail": detail, "has_secrets": secrets_present, **info}
        if dialect == "mysql":
            eng = create_engine(_mysql_url(db), pool_pre_ping=True,
                                connect_args={"connect_timeout": 5})
            try:
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                eng.dispose()
            return {"ok": True, "detail": "MySQL connection OK"}
        return {"ok": False, "error": f"Unsupported dialect: {db.dialect!r}"}
    except Exception as exc:  # noqa: BLE001 — report the failure to the wizard
        return {"ok": False, "error": str(exc)}


@router.post("/initialize")
async def initialize(payload: InitializePayload) -> dict:
    if _admin_exists():
        raise HTTPException(409, "Setup already completed.")

    interactive = bool(payload.base_dir and payload.base_dir.strip())

    # 1. Configure storage + secrets (interactive path) and (re)build the engine.
    if interactive:
        db_dict = _apply_storage_to_settings(payload.base_dir, payload.database)
        # Create db/, logs/, files/, … under the base dir before anything writes there
        # (a fresh Docker volume is empty → init_db would fail with "No such file…").
        runtime_paths.ensure_roots(payload.base_dir)
        runtime_paths.write_bootstrap(base_dir=payload.base_dir, database=db_dict)
        # Restore any original env secrets the operator supplied (adoption), then
        # honor an existing secrets.json and generate only what is still missing.
        if payload.secrets:
            runtime_paths.write_secrets(payload.secrets, base_dir=payload.base_dir)
        secrets = runtime_paths.load_or_create_secrets(generate=True, base_dir=payload.base_dir)
        settings.JWT_SECRET = secrets["JWT_SECRET"]
        settings.MAIL_SECRET_KEY = secrets["MAIL_SECRET_KEY"]
        settings.SETTINGS_ENCRYPTION_KEY = secrets["SETTINGS_ENCRYPTION_KEY"]
        settings.CONFIGURED = True
        settings.BASE_DIR = runtime_paths.resolve_roots(payload.base_dir)["base_dir"]
        # Drop cached crypto + engine so they pick up the new key/DB.
        from utils import crypto
        crypto._fernet.cache_clear()
        from db.database import reset_engine
        reset_engine()

    # 2. Create schema + base seeds (reuses the normal init path), then pull the
    #    DB-backed settings into the live config.
    from db.init_db import init_db
    await init_db()
    from db.database import get_session_factory
    from services import app_settings_registry

    db = get_session_factory()()
    try:
        app_settings_registry.hydrate(db)

        from models.authenticate import User
        from services.auth_service import hash_password
        from services.authz_service import normalize_roles

        # 3. Admin user — created only when the DB doesn't already have one
        #    (adoption of an existing database keeps its admins).
        admin_exists = any(
            any(str(r).strip().lower() == "admin" for r in (u.roles or []))
            for u in db.query(User).all()
        )
        if not admin_exists:
            if not payload.admin or not payload.admin.email.strip() or not payload.admin.password:
                raise HTTPException(400, "Admin email and password are required.")
            db.add(User(
                email=payload.admin.email.strip().lower(),
                password_hash=hash_password(payload.admin.password),
                is_active=True,
                roles=normalize_roles(["Admin"]),
            ))
            db.commit()

        # 4. Providers + models, then routing-slot assignments.
        from services.providers.registry_service import ProviderRegistryService
        from services.providers.capability_router import ALL_SLOTS
        from schemas.provider import ProviderCreate, ProviderModelCreate

        registry = ProviderRegistryService(db)
        model_pk_by_id: dict[str, int] = {}
        for prov in payload.providers:
            created = registry.create_provider(ProviderCreate(
                name=prov.name,
                provider_type=prov.provider_type,
                base_url=prov.base_url,
                enabled=True,
                is_local=prov.is_local,
                api_key=prov.api_key,
            ))
            for m in prov.models:
                pm = registry.create_model(ProviderModelCreate(
                    provider_id=created.id,
                    model_id=m.model_id,
                    capability=m.capability,
                    display_name=m.display_name,
                    enabled=True,
                    is_local=prov.is_local,
                ))
                model_pk_by_id[m.model_id] = pm.id

        for assign in payload.assignments:
            if assign.slot not in ALL_SLOTS:
                continue
            pk = model_pk_by_id.get(assign.model_id)
            if pk is not None:
                registry.set_assignment(assign.slot, pk)

        # 5. Optional tunable overrides → DB-backed settings registry.
        if payload.settings:
            app_settings_registry.apply_updates(db, payload.settings)
    finally:
        db.close()

    log.infox("First-time setup completed", interactive=interactive,
              providers=len(payload.providers), adopted=_admin_exists())
    # The process must restart so JWT_SECRET (read at import) + schedulers come up.
    _request_reload()
    return {"ok": True, "configured": True, "restart_required": True}


def _request_reload() -> None:
    """Best-effort: touch the server module so a uvicorn --reload dev process
    restarts into the configured phase. No-op effect under a production server,
    where the orchestrator restart honours restart_required."""
    try:
        server_py = Path(__file__).resolve().parent.parent / "server.py"
        os.utime(server_py, None)
    except Exception:
        pass
