from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from db.database import get_db
from schemas.transfer import (
    HostCreate, HostUpdate, HostRead,
    CredentialCreate, CredentialUpdate, CredentialRead,
    TransferRecordCreate, TransferRecordUpdate, TransferRecordRead,
    EndpointCreate, EndpointRead, ProgressEventRead,
    ParameterCreate, ParameterUpdate, ParameterRead,
    ConnectorDefCreate, ConnectorDefRead,
)
from services.transfer import transfer_engine
from services.transfer.connectors import CONNECTORS
from services.transfer.transfer_service import TransferService

# Ported Transfer-Hub: file-transfer routes (hosts/credentials/records). No scopes.
router = APIRouter(prefix="/transfer", tags=["transfer"], dependencies=[Depends(require_user)])


def _svc(db: Session = Depends(get_db)) -> TransferService:
    return TransferService(db)


# ── hosts ───────────────────────────────────────────────────────────────────────
@router.get("/hosts", response_model=list[HostRead])
def list_hosts(svc: TransferService = Depends(_svc)):
    return svc.list_hosts()


@router.post("/hosts", response_model=HostRead, status_code=201)
def create_host(body: HostCreate, svc: TransferService = Depends(_svc)):
    return svc.create_host(body)


@router.put("/hosts/{host_id}", response_model=HostRead)
def update_host(host_id: int, body: HostUpdate, svc: TransferService = Depends(_svc)):
    out = svc.update_host(host_id, body)
    if out is None:
        raise HTTPException(404, "Host not found")
    return out


@router.delete("/hosts/{host_id}")
def delete_host(host_id: int, svc: TransferService = Depends(_svc)):
    if not svc.delete_host(host_id):
        raise HTTPException(404, "Host not found")
    return {"deleted": True}


# ── credentials (secrets write-only) ──────────────────────────────────────────────
@router.get("/credentials", response_model=list[CredentialRead])
def list_credentials(svc: TransferService = Depends(_svc)):
    return svc.list_credentials()


@router.post("/credentials", response_model=CredentialRead, status_code=201)
def create_credential(body: CredentialCreate, svc: TransferService = Depends(_svc)):
    try:
        return svc.create_credential(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/credentials/{credential_id}", response_model=CredentialRead)
def update_credential(credential_id: int, body: CredentialUpdate, svc: TransferService = Depends(_svc)):
    out = svc.update_credential(credential_id, body)
    if out is None:
        raise HTTPException(404, "Credential not found")
    return out


@router.delete("/credentials/{credential_id}")
def delete_credential(credential_id: int, svc: TransferService = Depends(_svc)):
    if not svc.delete_credential(credential_id):
        raise HTTPException(404, "Credential not found")
    return {"deleted": True}


# ── transfer records (routes) ───────────────────────────────────────────────────
@router.get("/records", response_model=list[TransferRecordRead])
def list_records(svc: TransferService = Depends(_svc)):
    return svc.list_records()


@router.get("/records/{record_id}", response_model=TransferRecordRead)
def get_record(record_id: str, svc: TransferService = Depends(_svc)):
    out = svc.get_record_read(record_id)
    if out is None:
        raise HTTPException(404, "Transfer record not found")
    return out


@router.post("/records", response_model=TransferRecordRead, status_code=201)
def create_record(body: TransferRecordCreate, svc: TransferService = Depends(_svc)):
    try:
        return svc.create_record(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/records/{record_id}", response_model=TransferRecordRead)
def update_record(record_id: str, body: TransferRecordUpdate, svc: TransferService = Depends(_svc)):
    try:
        out = svc.update_record(record_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "Transfer record not found")
    return out


@router.post("/records/{record_id}/active", response_model=TransferRecordRead)
def activate_record(record_id: str, svc: TransferService = Depends(_svc)):
    out = svc.set_record_status(record_id, "ACTIVE")
    if out is None:
        raise HTTPException(404, "Transfer record not found")
    return out


@router.post("/records/{record_id}/inactive", response_model=TransferRecordRead)
def deactivate_record(record_id: str, svc: TransferService = Depends(_svc)):
    out = svc.set_record_status(record_id, "INACTIVE")
    if out is None:
        raise HTTPException(404, "Transfer record not found")
    return out


@router.delete("/records/{record_id}")
def delete_record(record_id: str, svc: TransferService = Depends(_svc)):
    if not svc.delete_record(record_id):
        raise HTTPException(404, "Transfer record not found")
    return {"deleted": True}


# ── endpoints (individual CRUD within a route) ────────────────────────────────────
@router.post("/records/{record_id}/endpoints", response_model=EndpointRead, status_code=201)
def add_endpoint(record_id: str, body: EndpointCreate, svc: TransferService = Depends(_svc)):
    try:
        out = svc.add_endpoint(record_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "Transfer record not found")
    return out


@router.put("/endpoints/{endpoint_id}", response_model=EndpointRead)
def update_endpoint(endpoint_id: int, body: EndpointCreate, svc: TransferService = Depends(_svc)):
    try:
        out = svc.update_endpoint(endpoint_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "Endpoint not found")
    return out


@router.delete("/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: int, svc: TransferService = Depends(_svc)):
    if not svc.delete_endpoint(endpoint_id):
        raise HTTPException(404, "Endpoint not found")
    return {"deleted": True}


# ── parameter catalog (defined, editable, add/remove) ─────────────────────────────
@router.get("/parameters", response_model=list[ParameterRead])
def list_parameters(svc: TransferService = Depends(_svc)):
    return svc.list_parameters()


@router.post("/parameters", response_model=ParameterRead, status_code=201)
def create_parameter(body: ParameterCreate, svc: TransferService = Depends(_svc)):
    try:
        return svc.create_parameter(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/parameters/{parameter_id}", response_model=ParameterRead)
def update_parameter(parameter_id: int, body: ParameterUpdate, svc: TransferService = Depends(_svc)):
    try:
        out = svc.update_parameter(parameter_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "Parameter not found")
    return out


@router.delete("/parameters/{parameter_id}")
def delete_parameter(parameter_id: int, svc: TransferService = Depends(_svc)):
    if not svc.delete_parameter(parameter_id):
        raise HTTPException(404, "Parameter not found")
    return {"deleted": True}


@router.get("/records/{record_id}/events", response_model=list[ProgressEventRead])
def record_events(record_id: str, svc: TransferService = Depends(_svc)):
    """Transfer history / monitoring log for a route (most recent first)."""
    return svc.list_events(record_id)


# ── execution engine ──────────────────────────────────────────────────────────────
class _EndpointTest(BaseModel):
    protocol: str
    host_id: int
    credential_id: int | None = None
    path: str | None = None


@router.get("/connectors")
def list_connectors():
    """Available transfer connectors (protocols) + the fields each needs."""
    return [{"protocol": p, "fields": cls.fields, "credential_type": cls.credential_type}
            for p, cls in sorted(CONNECTORS.items())]


# ── connector definitions (tier-2: add new connector TYPES at runtime) ────────────
@router.get("/connector-defs", response_model=list[ConnectorDefRead])
def list_connector_defs(svc: TransferService = Depends(_svc)):
    return svc.list_connector_defs()


@router.post("/connector-defs", response_model=ConnectorDefRead, status_code=201)
def create_connector_def(body: ConnectorDefCreate, svc: TransferService = Depends(_svc)):
    try:
        return svc.create_connector_def(body)
    except Exception as e:  # noqa: BLE001 — surface config/backend errors
        raise HTTPException(400, str(e))


@router.delete("/connector-defs/{def_id}")
def delete_connector_def(def_id: int, svc: TransferService = Depends(_svc)):
    if not svc.delete_connector_def(def_id):
        raise HTTPException(404, "Connector definition not found")
    return {"deleted": True}


@router.post("/endpoint/connection")
def test_endpoint_connection(body: _EndpointTest, db: Session = Depends(get_db)):
    return transfer_engine.test_endpoint(
        db, protocol=body.protocol, host_id=body.host_id,
        credential_id=body.credential_id, path=body.path,
    )


@router.post("/endpoint/browse")
def browse_endpoint(body: _EndpointTest, db: Session = Depends(get_db)):
    """List a directory on an endpoint (path navigation while building a route)."""
    return transfer_engine.browse_endpoint(
        db, protocol=body.protocol, host_id=body.host_id,
        credential_id=body.credential_id, path=body.path,
    )


# ── OneLake / Fabric helpers (workspace+lakehouse pickers, token identity) ─────────
class _OneLakeReq(BaseModel):
    credential_id: int | None = None  # None → use the host's az login
    workspace_id: str | None = None


def _onelake_secrets(db: Session, credential_id: int | None) -> dict:
    from models.transfer import Credential
    from services.transfer.transfer_engine import _secrets
    if credential_id is None:
        return {}  # → az login path
    cred = db.get(Credential, credential_id)
    return _secrets(cred)


@router.post("/onelake/workspaces")
def onelake_workspaces(body: _OneLakeReq, db: Session = Depends(get_db)):
    from services.transfer import onelake_fabric
    try:
        return {"ok": True, "workspaces": onelake_fabric.list_workspaces(_onelake_secrets(db, body.credential_id))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/onelake/lakehouses")
def onelake_lakehouses(body: _OneLakeReq, db: Session = Depends(get_db)):
    from services.transfer import onelake_fabric
    if not body.workspace_id:
        raise HTTPException(400, "workspace_id is required")
    try:
        return {"ok": True, "lakehouses": onelake_fabric.list_lakehouses(_onelake_secrets(db, body.credential_id), body.workspace_id)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/onelake/identity")
def onelake_identity(body: _OneLakeReq, db: Session = Depends(get_db)):
    """Whose token will be used (tenant + account/app) — verify it's the right org."""
    from services.transfer import onelake_fabric
    try:
        return onelake_fabric.identity(_onelake_secrets(db, body.credential_id))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/records/{record_id}/run")
def run_record(record_id: str, db: Session = Depends(get_db)):
    result = transfer_engine.run_record(db, record_id)
    if not result.get("ok") and result.get("error") == "transfer record not found":
        raise HTTPException(404, "Transfer record not found")
    return result
