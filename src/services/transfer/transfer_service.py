"""CRUD for the Transfer-Hub port: hosts, credentials, transfer records (routes)
and their endpoints, plus progress-event history. Credential secrets are encrypted
at rest and never returned. Execution engine lives in transfer_engine.py."""
from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from models.transfer import (
    Host, Credential, TransferRecord, Endpoint, ProgressEvent, Parameter, ConnectorDef,
    CREDENTIAL_TYPES, TRANSFER_DIRECTIONS, PARAMETER_TYPES, PARAMETER_DIRECTIONS,
)
from schemas.transfer import (
    HostCreate, HostUpdate, HostRead,
    CredentialCreate, CredentialUpdate, CredentialRead,
    TransferRecordCreate, TransferRecordUpdate, TransferRecordRead,
    EndpointCreate, EndpointRead, ProgressEventRead,
    ParameterCreate, ParameterUpdate, ParameterRead,
    ConnectorDefCreate, ConnectorDefRead,
)
from utils.crypto import encrypt_value

# Credential schema field -> model column for the encrypted secret material.
_SECRET_FIELDS = {
    "password": "password_encrypted",
    "public_key": "public_key_encrypted",
    "private_key": "private_key_encrypted",
    "key_phrase": "key_phrase_encrypted",
    "client_secret": "client_secret_encrypted",
    "token": "token_encrypted",
}


def _host_read(h: Host) -> HostRead:
    return HostRead(id=h.id, hostname=h.hostname, port=h.port, description=h.description)


def _credential_read(c: Credential) -> CredentialRead:
    has_secret = any(getattr(c, col) for col in _SECRET_FIELDS.values())
    return CredentialRead(
        id=c.id, credential_type=c.credential_type, name=c.name, username=c.username,
        client_id=c.client_id, tenant_id=c.tenant_id, has_secret=has_secret,
    )


def _endpoint_read(e: Endpoint) -> EndpointRead:
    return EndpointRead(
        id=e.id, protocol=e.protocol, path=e.path, direction=e.direction, parameter=e.parameter,
        host_id=e.host_id, credential_id=e.credential_id, transfer_record_id=e.transfer_record_id,
    )


def _record_read(r: TransferRecord) -> TransferRecordRead:
    return TransferRecordRead(
        id=r.id, description=r.description, status=r.status, version=r.version,
        schedule_cron=r.schedule_cron, last_run_at=r.last_run_at,
        endpoints=[_endpoint_read(e) for e in r.endpoints],
    )


class TransferService:
    def __init__(self, db: Session):
        self.db = db

    # ── hosts ─────────────────────────────────────────────────────────────────
    def list_hosts(self) -> List[HostRead]:
        return [_host_read(h) for h in self.db.query(Host).order_by(Host.id).all()]

    def create_host(self, data: HostCreate) -> HostRead:
        obj = Host(hostname=data.hostname, port=data.port, description=data.description)
        self.db.add(obj); self.db.commit(); self.db.refresh(obj)
        return _host_read(obj)

    def update_host(self, host_id: int, data: HostUpdate) -> Optional[HostRead]:
        obj = self.db.get(Host, host_id)
        if obj is None:
            return None
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(obj, k, v)
        self.db.commit(); self.db.refresh(obj)
        return _host_read(obj)

    def delete_host(self, host_id: int) -> bool:
        obj = self.db.get(Host, host_id)
        if obj is None:
            return False
        self.db.delete(obj); self.db.commit()
        return True

    # ── credentials (secrets encrypted) ────────────────────────────────────────
    def list_credentials(self) -> List[CredentialRead]:
        return [_credential_read(c) for c in self.db.query(Credential).order_by(Credential.id).all()]

    def _apply_secrets(self, obj: Credential, data: dict) -> None:
        for field, col in _SECRET_FIELDS.items():
            if field in data:
                val = data.pop(field)
                setattr(obj, col, encrypt_value(val) if val else None)

    def create_credential(self, data: CredentialCreate) -> CredentialRead:
        if data.credential_type not in CREDENTIAL_TYPES:
            raise ValueError(f"credential_type must be one of {CREDENTIAL_TYPES}")
        payload = data.model_dump()
        obj = Credential(
            credential_type=payload.pop("credential_type"), name=payload.get("name"),
            username=payload.get("username"), client_id=payload.get("client_id"),
            tenant_id=payload.get("tenant_id"),
        )
        self._apply_secrets(obj, payload)
        self.db.add(obj); self.db.commit(); self.db.refresh(obj)
        return _credential_read(obj)

    def update_credential(self, credential_id: int, data: CredentialUpdate) -> Optional[CredentialRead]:
        obj = self.db.get(Credential, credential_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        self._apply_secrets(obj, fields)  # pops secret keys
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.commit(); self.db.refresh(obj)
        return _credential_read(obj)

    def delete_credential(self, credential_id: int) -> bool:
        obj = self.db.get(Credential, credential_id)
        if obj is None:
            return False
        self.db.delete(obj); self.db.commit()
        return True

    # ── transfer records (routes) ──────────────────────────────────────────────
    def list_records(self) -> List[TransferRecordRead]:
        return [_record_read(r) for r in self.db.query(TransferRecord).order_by(TransferRecord.id).all()]

    def get_record(self, record_id: str) -> Optional[TransferRecord]:
        return self.db.get(TransferRecord, record_id)

    def get_record_read(self, record_id: str) -> Optional[TransferRecordRead]:
        r = self.get_record(record_id)
        return _record_read(r) if r else None

    def create_record(self, data: TransferRecordCreate) -> TransferRecordRead:
        for ep in data.endpoints:
            if ep.direction not in TRANSFER_DIRECTIONS:
                raise ValueError(f"endpoint.direction must be one of {TRANSFER_DIRECTIONS}")
        record = TransferRecord(id=str(uuid.uuid4()), description=data.description, status="INACTIVE", version=1,
                                schedule_cron=data.schedule_cron)
        self.db.add(record)
        self.db.flush()
        for ep in data.endpoints:
            self.db.add(Endpoint(
                protocol=ep.protocol, path=ep.path, direction=ep.direction, parameter=ep.parameter,
                host_id=ep.host_id, credential_id=ep.credential_id, transfer_record_id=record.id,
            ))
        self.db.commit(); self.db.refresh(record)
        return _record_read(record)

    def update_record(self, record_id: str, data: TransferRecordUpdate) -> Optional[TransferRecordRead]:
        obj = self.get_record(record_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        if "status" in fields and fields["status"] not in ("ACTIVE", "INACTIVE"):
            raise ValueError("status must be ACTIVE or INACTIVE")
        new_endpoints = fields.pop("endpoints", None)
        if new_endpoints is not None:
            for ep in data.endpoints or []:
                if ep.direction not in TRANSFER_DIRECTIONS:
                    raise ValueError(f"endpoint.direction must be one of {TRANSFER_DIRECTIONS}")
            for ex in list(obj.endpoints):
                self.db.delete(ex)
            self.db.flush()
            for ep in (data.endpoints or []):
                self.db.add(Endpoint(
                    protocol=ep.protocol, path=ep.path, direction=ep.direction, parameter=ep.parameter,
                    host_id=ep.host_id, credential_id=ep.credential_id, transfer_record_id=obj.id,
                ))
        for k, v in fields.items():
            setattr(obj, k, v)
        obj.version = (obj.version or 1) + 1
        self.db.commit(); self.db.refresh(obj)
        return _record_read(obj)

    def set_record_status(self, record_id: str, status: str) -> Optional[TransferRecordRead]:
        return self.update_record(record_id, TransferRecordUpdate(status=status))

    def delete_record(self, record_id: str) -> bool:
        obj = self.get_record(record_id)
        if obj is None:
            return False
        self.db.delete(obj); self.db.commit()
        return True

    # ── endpoints (individual CRUD within a route) ──────────────────────────────
    def add_endpoint(self, record_id: str, data: EndpointCreate) -> Optional[EndpointRead]:
        if self.get_record(record_id) is None:
            return None
        if data.direction not in TRANSFER_DIRECTIONS:
            raise ValueError(f"direction must be one of {TRANSFER_DIRECTIONS}")
        ep = Endpoint(
            protocol=data.protocol, path=data.path, direction=data.direction, parameter=data.parameter,
            host_id=data.host_id, credential_id=data.credential_id, transfer_record_id=record_id,
        )
        self.db.add(ep); self.db.commit(); self.db.refresh(ep)
        return _endpoint_read(ep)

    def update_endpoint(self, endpoint_id: int, data: EndpointCreate) -> Optional[EndpointRead]:
        ep = self.db.get(Endpoint, endpoint_id)
        if ep is None:
            return None
        if data.direction not in TRANSFER_DIRECTIONS:
            raise ValueError(f"direction must be one of {TRANSFER_DIRECTIONS}")
        for k, v in data.model_dump().items():
            setattr(ep, k, v)
        self.db.commit(); self.db.refresh(ep)
        return _endpoint_read(ep)

    def delete_endpoint(self, endpoint_id: int) -> bool:
        ep = self.db.get(Endpoint, endpoint_id)
        if ep is None:
            return False
        self.db.delete(ep); self.db.commit()
        return True

    # ── parameter catalog (defined, editable) ──────────────────────────────────
    def list_parameters(self) -> List[ParameterRead]:
        return [ParameterRead(id=p.id, name=p.name, display_name=p.display_name,
                              parameter_type=p.parameter_type, parameter_direction=p.parameter_direction)
                for p in self.db.query(Parameter).order_by(Parameter.name).all()]

    def create_parameter(self, data: ParameterCreate) -> ParameterRead:
        if data.parameter_type not in PARAMETER_TYPES:
            raise ValueError(f"parameter_type must be one of {PARAMETER_TYPES}")
        if data.parameter_direction not in PARAMETER_DIRECTIONS:
            raise ValueError(f"parameter_direction must be one of {PARAMETER_DIRECTIONS}")
        obj = Parameter(name=data.name, display_name=data.display_name,
                        parameter_type=data.parameter_type, parameter_direction=data.parameter_direction)
        self.db.add(obj); self.db.commit(); self.db.refresh(obj)
        return ParameterRead(id=obj.id, name=obj.name, display_name=obj.display_name,
                             parameter_type=obj.parameter_type, parameter_direction=obj.parameter_direction)

    def update_parameter(self, parameter_id: int, data: ParameterUpdate) -> Optional[ParameterRead]:
        obj = self.db.get(Parameter, parameter_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        if fields.get("parameter_type") and fields["parameter_type"] not in PARAMETER_TYPES:
            raise ValueError(f"parameter_type must be one of {PARAMETER_TYPES}")
        if fields.get("parameter_direction") and fields["parameter_direction"] not in PARAMETER_DIRECTIONS:
            raise ValueError(f"parameter_direction must be one of {PARAMETER_DIRECTIONS}")
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.commit(); self.db.refresh(obj)
        return ParameterRead(id=obj.id, name=obj.name, display_name=obj.display_name,
                             parameter_type=obj.parameter_type, parameter_direction=obj.parameter_direction)

    def delete_parameter(self, parameter_id: int) -> bool:
        obj = self.db.get(Parameter, parameter_id)
        if obj is None:
            return False
        self.db.delete(obj); self.db.commit()
        return True

    # ── connector definitions (tier-2: runtime-defined connector types) ─────────
    def list_connector_defs(self) -> List[ConnectorDefRead]:
        return [ConnectorDefRead(id=d.id, protocol=d.protocol, kind=d.kind, config=d.config,
                                 description=d.description, enabled=bool(d.enabled))
                for d in self.db.query(ConnectorDef).order_by(ConnectorDef.protocol).all()]

    def create_connector_def(self, data: ConnectorDefCreate) -> ConnectorDefRead:
        from services.transfer.connectors import register_def, CONNECTORS
        if data.protocol in CONNECTORS and self.db.query(ConnectorDef).filter(ConnectorDef.protocol == data.protocol).first() is None:
            raise ValueError(f"protocol '{data.protocol}' is already a built-in connector")
        register_def(data.protocol, data.kind, data.config)  # validate + register live (raises on bad config)
        obj = self.db.query(ConnectorDef).filter(ConnectorDef.protocol == data.protocol).first()
        if obj is None:
            obj = ConnectorDef(protocol=data.protocol, kind=data.kind, config=data.config, description=data.description, enabled=True)
            self.db.add(obj)
        else:
            obj.kind = data.kind; obj.config = data.config; obj.description = data.description; obj.enabled = True
        self.db.commit(); self.db.refresh(obj)
        return ConnectorDefRead(id=obj.id, protocol=obj.protocol, kind=obj.kind, config=obj.config, description=obj.description, enabled=bool(obj.enabled))

    def delete_connector_def(self, def_id: int) -> bool:
        from services.transfer.connectors import CONNECTORS, CONNECTOR_DEFS
        obj = self.db.get(ConnectorDef, def_id)
        if obj is None:
            return False
        CONNECTORS.pop(obj.protocol, None); CONNECTOR_DEFS.pop(obj.protocol, None)
        self.db.delete(obj); self.db.commit()
        return True

    def load_connector_defs(self) -> int:
        """Register all enabled connector defs into the live registry (boot)."""
        from services.transfer.connectors import register_def
        n = 0
        for d in self.db.query(ConnectorDef).filter(ConnectorDef.enabled == True).all():  # noqa: E712
            try:
                register_def(d.protocol, d.kind, d.config); n += 1
            except Exception:  # noqa: BLE001 — a bad/missing-backend def must not break others
                pass
        return n

    # ── monitoring ──────────────────────────────────────────────────────────────
    def list_events(self, record_id: str, limit: int = 100) -> List[ProgressEventRead]:
        rows = (self.db.query(ProgressEvent)
                .filter(ProgressEvent.transfer_record_id == record_id)
                .order_by(ProgressEvent.id.desc()).limit(limit).all())
        return [ProgressEventRead(
            id=e.id, case_number=e.case_number, transfer_record_id=e.transfer_record_id,
            from_host=e.from_host, to_host=e.to_host, file=e.file,
            progress_type=e.progress_type, exception_message=e.exception_message,
            processed_at=e.processed_at,
        ) for e in rows]
