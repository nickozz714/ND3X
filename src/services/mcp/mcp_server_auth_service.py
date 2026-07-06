from sqlalchemy.orm import Session
from fastapi import HTTPException

from component.logging import get_logger
from repository.mcp_server_repository import MCPServerRepository
from repository.mcp_server_auth_repository import MCPServerAuthRepository


log = get_logger(__name__)


class MCPServerAuthService:
    def __init__(self, db: Session):
        log.debugx(
            "MCPServerAuthService initialiseren",
            has_db_session=db is not None,
        )
        self.server_repo = MCPServerRepository(db)
        log.debugx("MCPServerRepository gekoppeld aan MCPServerAuthService")
        self.auth_repo = MCPServerAuthRepository(db)
        log.debugx("MCPServerAuthRepository gekoppeld aan MCPServerAuthService")

    def get_active_for_server(self, mcp_server_id: int):
        log.infox(
            "Actieve MCP server auth ophalen gestart",
            mcp_server_id=mcp_server_id,
        )
        server = self.server_repo.get_by_id(mcp_server_id)
        if not server:
            log.warningx(
                "Actieve MCP server auth ophalen mislukt: MCP server niet gevonden",
                mcp_server_id=mcp_server_id,
            )
            raise HTTPException(status_code=404, detail="MCP server not found")

        log.debugx(
            "MCP server gevonden voor actieve auth lookup",
            mcp_server_id=mcp_server_id,
            server_id=getattr(server, "id", None),
            server_name=getattr(server, "name", None),
            base_url=getattr(server, "base_url", None),
        )

        auth = self.auth_repo.get_active_for_server(mcp_server_id)
        if not auth:
            log.warningx(
                "Actieve MCP server auth niet gevonden",
                mcp_server_id=mcp_server_id,
                server_name=getattr(server, "name", None),
            )
            raise HTTPException(status_code=404, detail="Active MCP auth not found")

        log.infox(
            "Actieve MCP server auth ophalen afgerond",
            mcp_server_id=mcp_server_id,
            auth_id=getattr(auth, "id", None),
            auth_type=getattr(auth, "auth_type", None),
            is_active=getattr(auth, "is_active", None),
        )
        return auth

    def upsert_active_for_server(self, mcp_server_id: int, data):
        log.infox(
            "Actieve MCP server auth upsert gestart",
            mcp_server_id=mcp_server_id,
            auth_type=getattr(data, "auth_type", None),
            is_active=getattr(data, "is_active", None),
        )
        server = self.server_repo.get_by_id(mcp_server_id)
        if not server:
            log.warningx(
                "Actieve MCP server auth upsert mislukt: MCP server niet gevonden",
                mcp_server_id=mcp_server_id,
            )
            raise HTTPException(status_code=404, detail="MCP server not found")

        log.debugx(
            "MCP server gevonden voor auth upsert",
            mcp_server_id=mcp_server_id,
            server_id=getattr(server, "id", None),
            server_name=getattr(server, "name", None),
            base_url=getattr(server, "base_url", None),
        )

        auth = self.auth_repo.upsert_active_for_server(mcp_server_id, data)
        log.debugx(
            "Actieve MCP server auth upsert uitgevoerd",
            mcp_server_id=mcp_server_id,
            auth_id=getattr(auth, "id", None),
            auth_type=getattr(auth, "auth_type", None),
            is_active=getattr(auth, "is_active", None),
        )

        self.auth_repo.deactivate_others_for_server(mcp_server_id, auth.id)
        log.infox(
            "Andere MCP server auth records gedeactiveerd",
            mcp_server_id=mcp_server_id,
            active_auth_id=getattr(auth, "id", None),
        )

        log.infox(
            "Actieve MCP server auth upsert afgerond",
            mcp_server_id=mcp_server_id,
            auth_id=getattr(auth, "id", None),
            auth_type=getattr(auth, "auth_type", None),
        )
        return auth