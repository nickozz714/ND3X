from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from pathlib import Path

from authentication.dependencies import require_admin_user
from component.config import settings as app_settings
from db.database import get_db
from schemas.application_settings import (
    ApplicationSettingCreate,
    ApplicationSettingRead,
    ApplicationSettingUpdate,
)
from services import app_settings_registry
from services.application_setting_service import ApplicationSettingService


router = APIRouter(
    prefix="/admin/application-settings",
    tags=["Application settings"],
)


def get_service(db: Session = Depends(get_db)) -> ApplicationSettingService:
    return ApplicationSettingService(db)


class RegistryUpdate(BaseModel):
    settings: dict[str, str]


class BrowseUnderBase(BaseModel):
    subpath: str | None = None


@router.get("/registry/grouped")
def get_settings_registry(
    db: Session = Depends(get_db),
    _=Depends(require_admin_user),
):
    """The full DB-backed configuration, grouped for the settings UI. Secrets are
    masked (value blank + has_value flag). Path settings are relative to base_dir."""
    return {
        "groups": app_settings_registry.groups(db),
        "base_dir": app_settings.BASE_DIR or "",
        "managed_keys": app_settings_registry.managed_keys(),
    }


@router.post("/browse")
def browse_under_base(
    payload: BrowseUnderBase,
    _=Depends(require_admin_user),
):
    """Directory browser scoped to BASE_DIR, for picking path settings. Returns
    sub-paths relative to the base dir; never escapes above it."""
    base = (app_settings.BASE_DIR or "").strip()
    if not base:
        from fastapi import HTTPException
        raise HTTPException(400, "No base directory is configured.")
    base_p = Path(base).resolve()
    sub = (payload.subpath or "").strip().lstrip("/")
    target = (base_p / sub).resolve() if sub not in ("", ".") else base_p
    # Clamp inside the base dir.
    if target != base_p and base_p not in target.parents:
        target = base_p
    if not target.is_dir():
        target = base_p
    rel = "" if target == base_p else str(target.relative_to(base_p))
    parent = "" if target == base_p else (
        "" if target.parent == base_p else str(target.parent.relative_to(base_p))
    )
    dirs: list[str] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
            except OSError:
                continue
    except PermissionError:
        pass
    return {"path": rel, "parent": parent, "home": "", "dirs": dirs, "db_files": [], "base_dir": str(base_p)}


@router.put("/registry/grouped")
def update_settings_registry(
    data: RegistryUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin_user),
):
    """Bulk-upsert registry settings and re-hydrate the live config. Unknown keys
    and blank secrets are ignored."""
    written = app_settings_registry.apply_updates(db, data.settings)
    return {"ok": True, "written": written}


@router.get("", response_model=list[ApplicationSettingRead])
def get_application_settings(
    service: ApplicationSettingService = Depends(get_service),
):
    return service.get_all()


@router.get("/{key}", response_model=ApplicationSettingRead)
def get_application_setting(
    key: str,
    service: ApplicationSettingService = Depends(get_service),
):
    return service.get_by_key(key)


@router.post(
    "",
    response_model=ApplicationSettingRead,
    status_code=status.HTTP_201_CREATED,
)
def create_application_setting(
    data: ApplicationSettingCreate,
    service: ApplicationSettingService = Depends(get_service),
):
    return service.create(data)


@router.put("/{key}", response_model=ApplicationSettingRead)
def update_application_setting(
    key: str,
    data: ApplicationSettingUpdate,
    service: ApplicationSettingService = Depends(get_service),
):
    return service.update(key, data.value)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_application_setting(
    key: str,
    service: ApplicationSettingService = Depends(get_service),
):
    service.delete(key)