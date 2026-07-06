from sqlalchemy.orm import Session
from fastapi import HTTPException

from component.logging import get_logger
from repository.mcp_server_repository import MCPServerRepository


log = get_logger(__name__)

_STDIO_TYPES = {"stdio", "builtin"}


def _sanitize(data):
    """
    Zet lege base_url om naar een placeholder voor stdio/builtin servers.
    base_url is NOT NULL in de database maar niet relevant voor deze types.
    """
    server_type = getattr(data, "server_type", None) or "http"
    base_url = getattr(data, "base_url", None)

    if server_type in _STDIO_TYPES and not base_url:
        data.base_url = "stdio://local"

    return data


class MCPServerService:
    def __init__(self, db: Session):
        log.debugx(
            "MCPServerService initialiseren",
            has_db_session=db is not None,
        )
        self.repo = MCPServerRepository(db)
        log.debugx("MCPServerRepository gekoppeld aan MCPServerService")

    def get_all(self, skip: int = 0, limit: int = 100):
        log.infox("MCP servers ophalen gestart", skip=skip, limit=limit)
        result = self.repo.get_all(skip=skip, limit=limit)
        log.infox("MCP servers ophalen afgerond", skip=skip, limit=limit, count=len(result) if result is not None else None)
        return result

    def get_by_id(self, mcp_server_id: int):
        log.infox("MCP server ophalen op ID gestart", mcp_server_id=mcp_server_id)
        item = self.repo.get_by_id(mcp_server_id)
        if not item:
            log.warningx("MCP server niet gevonden op ID", mcp_server_id=mcp_server_id)
            raise HTTPException(status_code=404, detail="MCP server not found")
        log.infox(
            "MCP server ophalen op ID afgerond",
            mcp_server_id=mcp_server_id,
            found=True,
            name=getattr(item, "name", None),
            base_url=getattr(item, "base_url", None),
            is_enabled=getattr(item, "is_enabled", None),
        )
        return item

    def get_with_relations(self, mcp_server_id: int):
        log.infox("MCP server met relaties ophalen gestart", mcp_server_id=mcp_server_id)
        item = self.repo.get_with_relations(mcp_server_id)
        if not item:
            log.warningx("MCP server met relaties niet gevonden", mcp_server_id=mcp_server_id)
            raise HTTPException(status_code=404, detail="MCP server not found")
        log.infox(
            "MCP server met relaties ophalen afgerond",
            mcp_server_id=mcp_server_id,
            found=True,
            name=getattr(item, "name", None),
            base_url=getattr(item, "base_url", None),
            tool_count=len(getattr(item, "tools", []) or []) if hasattr(item, "tools") else None,
            auth_count=len(getattr(item, "auths", []) or []) if hasattr(item, "auths") else None,
        )
        return item

    def create(self, data):
        data = _sanitize(data)
        log.infox(
            "MCP server aanmaken gestart",
            name=getattr(data, "name", None),
            base_url=getattr(data, "base_url", None),
            server_type=getattr(data, "server_type", None),
            is_enabled=getattr(data, "is_enabled", None),
        )
        result = self.repo.create(data)
        log.infox(
            "MCP server aanmaken afgerond",
            mcp_server_id=getattr(result, "id", None),
            name=getattr(result, "name", None),
            base_url=getattr(result, "base_url", None),
            is_enabled=getattr(result, "is_enabled", None),
        )
        return result

    def update(self, mcp_server_id: int, data):
        data = _sanitize(data)
        log.infox(
            "MCP server bijwerken gestart",
            mcp_server_id=mcp_server_id,
            name=getattr(data, "name", None),
            base_url=getattr(data, "base_url", None),
            server_type=getattr(data, "server_type", None),
            is_enabled=getattr(data, "is_enabled", None),
        )
        item = self.repo.update(mcp_server_id, data)
        if not item:
            log.warningx("MCP server niet gevonden voor update", mcp_server_id=mcp_server_id)
            raise HTTPException(status_code=404, detail="MCP server not found")
        log.infox(
            "MCP server bijwerken afgerond",
            mcp_server_id=mcp_server_id,
            result_id=getattr(item, "id", None),
            name=getattr(item, "name", None),
            base_url=getattr(item, "base_url", None),
            is_enabled=getattr(item, "is_enabled", None),
        )
        return item

    def delete(self, mcp_server_id: int):
        log.infox("MCP server verwijderen gestart", mcp_server_id=mcp_server_id)
        ok = self.repo.delete(mcp_server_id)
        if not ok:
            log.warningx("MCP server niet gevonden voor verwijderen", mcp_server_id=mcp_server_id)
            raise HTTPException(status_code=404, detail="MCP server not found")
        log.infox("MCP server verwijderen afgerond", mcp_server_id=mcp_server_id, success=ok)
        return {"success": True}
