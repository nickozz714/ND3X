"""
routers/secrets.py

Native, encrypted secret store (KeyVault). Replaces the MCP-proxied keyvault for
in-project use. The plaintext value is never returned: reads expose metadata, the
value endpoint returns an obfuscated form only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from component.logging import get_logger
from db.database import get_db
from schemas.secret import (
    DeleteResponse,
    ImportEnvRequest,
    ImportEnvResult,
    SecretCreate,
    SecretMetadata,
    SecretUpdate,
    SecretValueObfuscated,
)
from services.authz_service import assert_expert_role
from services.secret_service import SecretError, SecretService

log = get_logger(__name__)

router = APIRouter(prefix="/admin/secrets", tags=["admin-secrets"])


def _meta(row) -> SecretMetadata:
    return SecretMetadata(
        name=row.name,
        description=row.description,
        tags=list(row.tags or []),
        placeholder=bool(row.placeholder),
        has_value=row.value_encrypted is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[SecretMetadata])
def list_secrets(db: Session = Depends(get_db), user=Depends(require_user)):
    return [_meta(r) for r in SecretService(db).list()]


@router.post("/import-env", response_model=ImportEnvResult)
def import_env(body: ImportEnvRequest, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        return ImportEnvResult(**SecretService(db).import_env(body.content, overwrite=body.overwrite))
    except SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{name}", response_model=SecretMetadata)
def get_secret(name: str, db: Session = Depends(get_db), user=Depends(require_user)):
    row = SecretService(db).get(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
    return _meta(row)


@router.get("/{name}/value", response_model=SecretValueObfuscated)
def get_secret_value(name: str, db: Session = Depends(get_db), user=Depends(require_user)):
    svc = SecretService(db)
    row = svc.get(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
    return SecretValueObfuscated(
        name=name,
        value_obfuscated=svc.get_value_obfuscated(name),
        has_value=row.value_encrypted is not None,
    )


@router.post("", response_model=SecretMetadata, status_code=201)
def create_secret(data: SecretCreate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        return _meta(SecretService(db).create(data))
    except SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{name}", response_model=SecretMetadata)
def update_secret(name: str, data: SecretUpdate, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        return _meta(SecretService(db).update(name, data))
    except SecretError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc))


@router.delete("/{name}", response_model=DeleteResponse)
def delete_secret(name: str, db: Session = Depends(get_db), user=Depends(require_user)):
    assert_expert_role(user)
    try:
        SecretService(db).delete(name)
    except SecretError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return DeleteResponse(ok=True, deleted=name)
