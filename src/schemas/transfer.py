"""Request/response schemas for the Transfer-Hub port (scopes removed).

Credential secrets are write-only (accepted on create/update, never returned);
reads expose only `has_secret` + non-secret fields.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


# ── Host ──────────────────────────────────────────────────────────────────────
class HostBase(BaseModel):
    hostname: str
    port: Optional[int] = None
    description: Optional[str] = None


class HostCreate(HostBase):
    pass


class HostUpdate(BaseModel):
    hostname: Optional[str] = None
    port: Optional[int] = None
    description: Optional[str] = None


class HostRead(HostBase):
    id: int


# ── Credential (secrets write-only) ───────────────────────────────────────────
class CredentialBase(BaseModel):
    credential_type: str  # SFTP|FILE|OAUTH|SAS_TOKEN|ACCESS_KEY
    name: Optional[str] = None
    username: Optional[str] = None
    client_id: Optional[str] = None
    tenant_id: Optional[str] = None


class CredentialCreate(CredentialBase):
    password: Optional[str] = None
    public_key: Optional[str] = None
    private_key: Optional[str] = None
    key_phrase: Optional[str] = None
    client_secret: Optional[str] = None
    token: Optional[str] = None


class CredentialUpdate(BaseModel):
    credential_type: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None
    client_id: Optional[str] = None
    tenant_id: Optional[str] = None
    password: Optional[str] = None
    public_key: Optional[str] = None
    private_key: Optional[str] = None
    key_phrase: Optional[str] = None
    client_secret: Optional[str] = None
    token: Optional[str] = None


class CredentialRead(CredentialBase):
    id: int
    has_secret: bool = False


# ── Endpoint ──────────────────────────────────────────────────────────────────
class EndpointBase(BaseModel):
    protocol: str
    path: Optional[str] = None
    direction: str  # FROM|TO
    parameter: Optional[str] = None
    host_id: int
    credential_id: Optional[int] = None


class EndpointCreate(EndpointBase):
    pass


class EndpointRead(EndpointBase):
    id: int
    transfer_record_id: str


# ── TransferRecord (a route + its endpoints) ──────────────────────────────────
class TransferRecordBase(BaseModel):
    description: Optional[str] = None


class TransferRecordCreate(TransferRecordBase):
    endpoints: List[EndpointCreate] = []
    schedule_cron: Optional[str] = None


class TransferRecordUpdate(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = None  # ACTIVE|INACTIVE
    endpoints: Optional[List[EndpointCreate]] = None  # when given, replaces all endpoints
    schedule_cron: Optional[str] = None


class TransferRecordRead(TransferRecordBase):
    id: str
    status: str
    version: int
    schedule_cron: Optional[str] = None
    last_run_at: Optional[datetime] = None
    endpoints: List[EndpointRead] = []


# ── Connector definitions (tier-2: runtime-defined connector types) ───────────
class ConnectorDefBase(BaseModel):
    protocol: str
    kind: str  # fsspec | rest
    config: Optional[dict] = None
    description: Optional[str] = None


class ConnectorDefCreate(ConnectorDefBase):
    pass


class ConnectorDefRead(ConnectorDefBase):
    id: int
    enabled: bool = True


# ── Parameter catalog (defined, editable connector parameters) ────────────────
class ParameterBase(BaseModel):
    name: str
    display_name: Optional[str] = None
    parameter_type: str = "VALUE"        # VALUE|BOOLEAN
    parameter_direction: str = "BOTH"    # FROM|TO|BOTH


class ParameterCreate(ParameterBase):
    pass


class ParameterUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    parameter_type: Optional[str] = None
    parameter_direction: Optional[str] = None


class ParameterRead(ParameterBase):
    id: int


class ProgressEventRead(BaseModel):
    id: int
    case_number: Optional[str] = None
    transfer_record_id: str
    from_host: Optional[str] = None
    to_host: Optional[str] = None
    file: Optional[str] = None
    progress_type: str
    exception_message: Optional[str] = None
    processed_at: Optional[datetime] = None
