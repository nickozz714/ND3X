"""Transfer execution engine: resolve a connector per endpoint (decrypting its
credential at run time), test connectivity, and run a transfer record (read from
FROM endpoints → write to TO endpoints). Mirrors the Camel route-factory flow,
kept dynamic so new connectors/locations slot in without engine changes.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.transfer import TransferRecord, Endpoint, Credential, Host, ProgressEvent
from services.transfer.connectors import CONNECTORS, Connector
from utils.crypto import decrypt_value

log = get_logger(__name__)

# credential field -> encrypted column
_SECRET_COLS = {
    "password": "password_encrypted",
    "private_key": "private_key_encrypted",
    "key_phrase": "key_phrase_encrypted",
    "client_secret": "client_secret_encrypted",
    "token": "token_encrypted",
    "public_key": "public_key_encrypted",
}


def _secrets(cred: Optional[Credential]) -> Dict[str, str]:
    if cred is None:
        return {}
    out: Dict[str, Any] = {"username": cred.username, "client_id": cred.client_id, "tenant_id": cred.tenant_id}
    for field, col in _SECRET_COLS.items():
        enc = getattr(cred, col, None)
        if enc:
            out[field] = decrypt_value(enc)
    return {k: v for k, v in out.items() if v}


def _params(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001 — params are best-effort
        return {}


def _basename(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").split("/")[-1]


def _target_path(te_path: Optional[str], source_file: str, source_is_dir: bool) -> str:
    """Where to write a source file on a TO endpoint. Drop the file (by basename) into
    the TO directory when the source is a directory OR the TO path is a folder (ends
    with '/'); otherwise the TO path is the exact file path."""
    if not te_path:
        return source_file
    if source_is_dir or te_path.endswith("/"):
        return te_path.rstrip("/") + "/" + _basename(source_file)
    return te_path


def _event(db: Session, record_id: str, *, ptype: str, file: Optional[str] = None,
           from_host: Optional[str] = None, to_host: Optional[str] = None, error: Optional[str] = None) -> None:
    db.add(ProgressEvent(
        case_number=str(uuid.uuid4())[:8], transfer_record_id=record_id, progress_type=ptype,
        file=file, from_host=from_host, to_host=to_host, exception_message=error,
    ))


def build_connector(endpoint: Endpoint, host: Optional[Host], cred: Optional[Credential]) -> Connector:
    cls = CONNECTORS.get(endpoint.protocol)
    if cls is None:
        raise RuntimeError(f"No connector registered for protocol '{endpoint.protocol}'. Available: {sorted(CONNECTORS)}")
    conn = cls(
        hostname=host.hostname if host else None,
        port=host.port if host else None,
        path=endpoint.path,
        params=_params(endpoint.parameter),
        secrets=_secrets(cred),
    )
    # The endpoint's protocol is authoritative (one shared class can back several
    # fsspec protocols), so the instance knows exactly which backend to use.
    conn.protocol = endpoint.protocol
    return conn


def test_endpoint(db: Session, *, protocol: str, host_id: int,
                  credential_id: Optional[int], path: Optional[str]) -> dict:
    host = db.get(Host, host_id)
    if host is None:
        return {"ok": False, "error": "host not found"}
    cred = db.get(Credential, credential_id) if credential_id else None
    ep = Endpoint(protocol=protocol, path=path, direction="FROM", host_id=host_id, credential_id=credential_id)
    try:
        build_connector(ep, host, cred).test_connection()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — surface a clean message
        return {"ok": False, "error": str(e)}


def browse_endpoint(db: Session, *, protocol: str, host_id: int,
                    credential_id: Optional[int], path: Optional[str]) -> dict:
    """List a directory on an endpoint for the UI path browser."""
    host = db.get(Host, host_id)
    if host is None:
        return {"ok": False, "error": "host not found"}
    cred = db.get(Credential, credential_id) if credential_id else None
    ep = Endpoint(protocol=protocol, path=path, direction="FROM", host_id=host_id, credential_id=credential_id)
    try:
        entries = build_connector(ep, host, cred).browse(path or "")
        return {"ok": True, "path": path or "", "entries": entries}
    except NotImplementedError:
        return {"ok": False, "error": f"browsing isn't supported for '{protocol}' — type the path manually."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def run_record(db: Session, record_id: str) -> dict:
    """Execute a route: read each FROM endpoint, write to each TO endpoint."""
    rec = db.get(TransferRecord, record_id)
    if rec is None:
        return {"ok": False, "error": "transfer record not found"}
    froms = [e for e in rec.endpoints if e.direction == "FROM"]
    tos = [e for e in rec.endpoints if e.direction == "TO"]
    if not froms or not tos:
        return {"ok": False, "error": "route needs at least one FROM and one TO endpoint"}

    transferred: List[dict] = []
    try:
        for fe in froms:
            src = build_connector(fe, fe.host, fe.credential)
            is_dir = src.is_directory(fe.path)
            for sf in src.iter_files(fe.path):
                data = src.read(sf)
                for te in tos:
                    dst = build_connector(te, te.host, te.credential)
                    target = _target_path(te.path, sf, is_dir)
                    dst.write(target, data)
                    transferred.append({"from": sf, "to": target, "bytes": len(data)})
                    _event(db, record_id, ptype="COMPLETED", file=sf,
                           from_host=fe.host.hostname if fe.host else None,
                           to_host=te.host.hostname if te.host else None)
        db.commit()
        log.infox("Transfer record uitgevoerd", record_id=record_id, files=len(transferred))
        return {"ok": True, "transferred": transferred}
    except Exception as e:  # noqa: BLE001
        rec.status = "ERROR"
        _event(db, record_id, ptype="FAILED", error=str(e))
        db.commit()
        log.warningx("Transfer record mislukt", record_id=record_id, error=str(e))
        return {"ok": False, "error": str(e), "transferred": transferred}


def poll_record(db: Session, rec: TransferRecord) -> dict:
    """Move semantics for the scheduler: if a FROM source exists, read → write to
    every TO → delete the source (so it isn't re-transferred next tick)."""
    froms = [e for e in rec.endpoints if e.direction == "FROM"]
    tos = [e for e in rec.endpoints if e.direction == "TO"]
    if not froms or not tos:
        return {"ok": False, "skipped": "route needs FROM and TO endpoints"}
    moved: List[str] = []
    for fe in froms:
        src = build_connector(fe, fe.host, fe.credential)
        is_dir = src.is_directory(fe.path)
        try:
            files = src.iter_files(fe.path)  # [] when nothing is waiting
        except NotImplementedError:
            return {"ok": False, "skipped": f"protocol '{fe.protocol}' has no iter_files() for polling — use manual run"}
        for sf in files:
            data = src.read(sf)
            for te in tos:
                build_connector(te, te.host, te.credential).write(_target_path(te.path, sf, is_dir), data)
                _event(db, rec.id, ptype="COMPLETED", file=sf,
                       from_host=fe.host.hostname if fe.host else None,
                       to_host=te.host.hostname if te.host else None)
            try:
                src.delete(sf)
            except NotImplementedError:
                return {"ok": False, "error": f"protocol '{fe.protocol}' has no delete(); would re-transfer — skipping move"}
            moved.append(sf)
    return {"ok": True, "moved": moved}


def poll_active(db: Session) -> dict:
    """One poll pass over ACTIVE records WITHOUT a cron schedule (continuous watcher:
    move files when present). Cron-scheduled records are handled by run_scheduled."""
    recs = (db.query(TransferRecord)
            .filter(TransferRecord.status == "ACTIVE",
                    (TransferRecord.schedule_cron.is_(None)) | (TransferRecord.schedule_cron == ""))
            .all())
    results: Dict[str, dict] = {}
    for r in recs:
        try:
            results[r.id] = poll_record(db, r)
        except Exception as e:  # noqa: BLE001
            r.status = "ERROR"
            _event(db, r.id, ptype="FAILED", error=str(e))
            results[r.id] = {"ok": False, "error": str(e)}
            log.warningx("Transfer poll mislukt", record_id=r.id, error=str(e))
    db.commit()
    return results


def run_scheduled(db: Session) -> dict:
    """Run ACTIVE records whose cron schedule is due (move semantics). Each is armed
    on first sight (last_run_at set) and fires at subsequent cron boundaries."""
    from datetime import datetime, timezone
    from croniter import croniter

    now = datetime.now(timezone.utc)
    recs = (db.query(TransferRecord)
            .filter(TransferRecord.status == "ACTIVE",
                    TransferRecord.schedule_cron.isnot(None), TransferRecord.schedule_cron != "")
            .all())
    results: Dict[str, dict] = {}
    for r in recs:
        try:
            if r.last_run_at is None:
                r.last_run_at = now  # arm: first run at the next cron boundary
                continue
            base = r.last_run_at
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            if not croniter.is_valid(r.schedule_cron):
                results[r.id] = {"ok": False, "error": f"invalid cron '{r.schedule_cron}'"}
                continue
            due = croniter(r.schedule_cron, base).get_next(datetime) <= now
            if due:
                results[r.id] = poll_record(db, r)
                r.last_run_at = now
        except Exception as e:  # noqa: BLE001
            r.status = "ERROR"
            _event(db, r.id, ptype="FAILED", error=f"schedule: {e}")
            results[r.id] = {"ok": False, "error": str(e)}
            log.warningx("Scheduled transfer mislukt", record_id=r.id, error=str(e))
    db.commit()
    return results
