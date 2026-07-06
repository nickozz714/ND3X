"""Transfer-Hub domain ported into ND3X (file-transfer orchestration).

  Host ──< Endpoint >── Credential
              │
  TransferRecord ──< Endpoint        (a record/route groups its endpoints; Direction FROM/TO)

A TransferRecord is a route: FROM endpoints (pick-up) + TO endpoints (drop-off),
each bound to a Host + Credential. The engine builds/executes the route from these
at run time. Credential secrets are Fernet-encrypted. (Scopes were dropped — not
used here; hosts/credentials/routes are global.)
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.database import Base

# Enums (mirrors com.definefunction.transfer.model.pojo.*)
TRANSFER_DIRECTIONS = ("FROM", "TO")
CREDENTIAL_TYPES = ("SFTP", "FILE", "OAUTH", "SAS_TOKEN", "ACCESS_KEY")
TRANSFER_STATUS = ("ACTIVE", "INACTIVE", "ERROR", "FAILED")
PROGRESS_TYPES = ("INITIATED", "PROCESSING", "COMPLETED", "FAILED")
PARAMETER_TYPES = ("VALUE", "BOOLEAN")
PARAMETER_DIRECTIONS = ("FROM", "TO", "BOTH")


class Host(Base):
    __tablename__ = "transfer_hosts"

    id = Column(Integer, primary_key=True, index=True)
    hostname = Column(String(255), nullable=False)
    port = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    endpoints = relationship("Endpoint", back_populates="host", cascade="all, delete-orphan")


class Credential(Base):
    """Typed credential referenced by endpoints. Secret fields are stored
    Fernet-encrypted; never returned in API responses (write-only)."""
    __tablename__ = "transfer_credentials"

    id = Column(Integer, primary_key=True, index=True)
    credential_type = Column(String(32), nullable=False)  # SFTP|FILE|OAUTH|SAS_TOKEN|ACCESS_KEY
    name = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    # encrypted secret material
    password_encrypted = Column(Text, nullable=True)
    public_key_encrypted = Column(Text, nullable=True)
    private_key_encrypted = Column(Text, nullable=True)
    key_phrase_encrypted = Column(Text, nullable=True)
    client_id = Column(String(255), nullable=True)
    client_secret_encrypted = Column(Text, nullable=True)
    tenant_id = Column(String(255), nullable=True)
    token_encrypted = Column(Text, nullable=True)
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    endpoints = relationship("Endpoint", back_populates="credential")


class TransferRecord(Base):
    """A route: groups its endpoints (FROM/TO)."""
    __tablename__ = "transfer_records"

    id = Column(String(64), primary_key=True, index=True)  # string id (e.g. uuid)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="INACTIVE")  # ACTIVE|INACTIVE|ERROR|FAILED
    version = Column(Integer, nullable=False, default=1)
    # Optional cron schedule (5-field). When set on an ACTIVE record, the route runs
    # on that schedule instead of the continuous 20s watcher. Empty/None → watcher.
    schedule_cron = Column(String(120), nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    endpoints = relationship("Endpoint", back_populates="transfer_record", cascade="all, delete-orphan")


class Endpoint(Base):
    """One side of a transfer: protocol + host + path + direction + credential."""
    __tablename__ = "transfer_endpoints"

    id = Column(Integer, primary_key=True, index=True)
    protocol = Column(String(64), nullable=False)        # sftp|file|azure-storage-blob|...
    path = Column(Text, nullable=True)
    direction = Column(String(8), nullable=False)        # FROM|TO
    parameter = Column(Text, nullable=True)              # extra connector params (query/opts)
    host_id = Column(Integer, ForeignKey("transfer_hosts.id"), nullable=False)
    credential_id = Column(Integer, ForeignKey("transfer_credentials.id"), nullable=True)
    transfer_record_id = Column(String(64), ForeignKey("transfer_records.id"), nullable=False)
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    host = relationship("Host", back_populates="endpoints")
    credential = relationship("Credential", back_populates="endpoints")
    transfer_record = relationship("TransferRecord", back_populates="endpoints")


class Parameter(Base):
    """Catalog of connector parameters that can be applied to endpoints (defined,
    reusable). parameter_type VALUE takes a value; BOOLEAN is a flag."""
    __tablename__ = "transfer_parameters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)            # the connector option key (e.g. "delay")
    display_name = Column(String(255), nullable=True)
    parameter_type = Column(String(16), nullable=False, default="VALUE")       # VALUE|BOOLEAN
    parameter_direction = Column(String(8), nullable=False, default="BOTH")    # FROM|TO|BOTH
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ConnectorDef(Base):
    """A runtime-defined connector type (tier-2 self-extending). DECLARATIVE only —
    no code is generated/executed: kind 'fsspec' binds the generic fsspec connector
    to a protocol; kind 'rest' configures the templated HTTP connector. The LLM (or
    an admin) can add new location TYPES this way; built-in connectors are unaffected.
    """
    __tablename__ = "transfer_connector_defs"

    id = Column(Integer, primary_key=True, index=True)
    protocol = Column(String(64), unique=True, nullable=False, index=True)  # the name to register
    kind = Column(String(16), nullable=False)        # fsspec | rest
    config = Column(JSON, nullable=True)             # kind-specific declarative config
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProgressEvent(Base):
    """Transfer history / monitoring log (one row per file outcome)."""
    __tablename__ = "transfer_progress_events"

    id = Column(Integer, primary_key=True, index=True)
    case_number = Column(String(64), nullable=True)
    transfer_record_id = Column(String(64), ForeignKey("transfer_records.id"), nullable=False, index=True)
    from_host = Column(String(255), nullable=True)
    to_host = Column(String(255), nullable=True)
    file = Column(Text, nullable=True)
    progress_type = Column(String(24), nullable=False)  # INITIATED|PROCESSING|COMPLETED|FAILED
    exception_message = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
