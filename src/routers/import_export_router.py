"""
routers/import_export_router.py

Portable export/import for Skills, MCP Servers, Workflows and Meeting Profiles —
per item (?ids=) or in bulk. Managing config requires the Expert role.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from services.authz_service import assert_expert_role
from services.import_export_service import (
    KINDS,
    ImportExportError,
    export,
    import_envelope,
)

router = APIRouter(prefix="/admin/import-export", tags=["Import/Export"])


@router.get("/{kind}")
def export_kind(
    kind: str,
    ids: Optional[str] = Query(None, description="Comma-separated ids; omit for all."),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> Dict[str, Any]:
    assert_expert_role(user)
    id_list: Optional[List[int]] = None
    if ids:
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
    try:
        return export(db, kind, id_list)
    except ImportExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{kind}")
def import_kind(
    kind: str,
    envelope: Dict[str, Any],
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> Dict[str, Any]:
    assert_expert_role(user)
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'")
    # Tolerate a bare items list or a mismatched-but-present kind: trust the URL.
    envelope = dict(envelope or {})
    envelope.setdefault("kind", kind)
    if envelope.get("kind") != kind:
        raise HTTPException(
            status_code=400,
            detail=f"File is a '{envelope.get('kind')}' export, not '{kind}'.",
        )
    try:
        return import_envelope(db, envelope, user=user)
    except ImportExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
