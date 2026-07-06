"""Microsoft Fabric Data Agent registrations.

A Fabric Data Agent answers natural-language questions grounded in Fabric data
(lakehouse/warehouse/semantic models) via an OpenAI-compatible endpoint:
  https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/aiskills/{data_agent_id}/aiassistant/openai

The orchestrator queries these through the `fabric_data_agent_query` builtin tool.
Auth is per-agent: a service principal, the reused Azure login session, or a
stored bearer token. Secrets are Fernet-encrypted; never returned in read models.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from db.database import Base

# Supported per-agent authentication methods.
# interactive_browser is only usable in the desktop app (loopback redirect).
FABRIC_AUTH_METHODS = ("service_principal", "azure_login", "interactive_browser", "bearer_token")


class FabricDataAgent(Base):
    __tablename__ = "fabric_data_agents"

    id = Column(Integer, primary_key=True, index=True)
    # Short stable slug the orchestrator passes as `agent` (e.g. "sales").
    name = Column(String(120), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    # What data this agent covers — shown in the manifest so the agent picks the right one.
    description = Column(Text, nullable=True)

    workspace_id = Column(String(128), nullable=False)
    data_agent_id = Column(String(128), nullable=False)  # the aiskill/data-agent id
    api_version = Column(String(64), nullable=True)       # optional ?api-version override

    # "service_principal" | "azure_login" | "bearer_token"
    auth_method = Column(String(32), nullable=False, default="azure_login")
    tenant_id = Column(String(128), nullable=True)
    client_id = Column(String(128), nullable=True)
    client_secret_encrypted = Column(Text, nullable=True)  # service principal secret
    bearer_token_encrypted = Column(Text, nullable=True)   # stored bearer token

    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
