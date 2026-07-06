import logging
from sqlalchemy.orm import Session
from repository.tool_repository import ToolRepository
from schemas.tool import ToolCreate, ToolUpdate
from fastapi import HTTPException
from component.logging import get_logger

logger = logging.getLogger(__name__)
log = get_logger(__name__)


class ToolService:
    def __init__(self, db: Session):
        log.debugx(
            "ToolService initialiseren",
            has_db_session=db is not None,
        )
        self.repository = ToolRepository(db)
        log.debugx("ToolRepository gekoppeld aan ToolService")

    def get_all(self, skip: int = 0, limit: int = 100):
        logger.debug("Service: get_all tool")
        log.infox(
            "Tools ophalen gestart",
            skip=skip,
            limit=limit,
        )
        result = self.repository.get_all(skip=skip, limit=limit)
        log.infox(
            "Tools ophalen afgerond",
            skip=skip,
            limit=limit,
            count=len(result) if result is not None else None,
        )
        return result

    def get_by_id(self, id: int):
        log.infox(
            "Tool ophalen op ID gestart",
            tool_id=id,
        )
        obj = self.repository.get_by_id(id)
        if not obj:
            logger.warning("Tool not found: id=%s", id)
            log.warningx(
                "Tool niet gevonden op ID",
                tool_id=id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")
        log.infox(
            "Tool ophalen op ID afgerond",
            tool_id=id,
            found=True,
            name=getattr(obj, "name", None),
            type=getattr(obj, "type", None),
            is_enabled=getattr(obj, "is_enabled", None),
        )
        return obj

    def get_with_relations(self, id: int):
        log.infox(
            "Tool met relaties ophalen gestart",
            tool_id=id,
        )
        obj = self.repository.get_with_relations(id)
        if not obj:
            logger.warning("Tool not found: id=%s", id)
            log.warningx(
                "Tool met relaties niet gevonden",
                tool_id=id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")
        log.infox(
            "Tool met relaties ophalen afgerond",
            tool_id=id,
            found=True,
            name=getattr(obj, "name", None),
            type=getattr(obj, "type", None),
            assistants_count=len(getattr(obj, "assistants", []) or []) if hasattr(obj, "assistants") else None,
        )
        return obj

    def get_all_with_relations(self, skip: int = 0, limit: int = 100):
        logger.debug("Service: get_all_with_relations tool")
        log.infox(
            "Tools met relaties ophalen gestart",
            skip=skip,
            limit=limit,
        )
        result = self.repository.get_all_with_relations(skip=skip, limit=limit)
        log.infox(
            "Tools met relaties ophalen afgerond",
            skip=skip,
            limit=limit,
            count=len(result) if result is not None else None,
        )
        return result

    def create(self, data: ToolCreate):
        logger.info("Service: creating tool")
        log.infox(
            "Tool aanmaken gestart",
            name=getattr(data, "name", None),
            type=getattr(data, "type", None),
            is_enabled=getattr(data, "is_enabled", None),
            mcp_server_id=getattr(data, "mcp_server_id", None),
        )
        result = self.repository.create(data)
        log.infox(
            "Tool aanmaken afgerond",
            tool_id=getattr(result, "id", None),
            name=getattr(result, "name", None),
            type=getattr(result, "type", None),
            is_enabled=getattr(result, "is_enabled", None),
        )
        return result

    def update(self, id: int, data: ToolUpdate):
        log.infox(
            "Tool bijwerken gestart",
            tool_id=id,
            name=getattr(data, "name", None),
            type=getattr(data, "type", None),
            is_enabled=getattr(data, "is_enabled", None),
            mcp_server_id=getattr(data, "mcp_server_id", None),
        )
        obj = self.repository.update(id, data)
        if not obj:
            logger.warning("Tool not found for update: id=%s", id)
            log.warningx(
                "Tool niet gevonden voor update",
                tool_id=id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")
        log.infox(
            "Tool bijwerken afgerond",
            tool_id=id,
            result_id=getattr(obj, "id", None),
            name=getattr(obj, "name", None),
            type=getattr(obj, "type", None),
            is_enabled=getattr(obj, "is_enabled", None),
        )
        return obj

    def delete(self, id: int):
        log.infox(
            "Tool verwijderen gestart",
            tool_id=id,
        )
        success = self.repository.delete(id)
        if not success:
            logger.warning("Tool not found for delete: id=%s", id)
            log.warningx(
                "Tool niet gevonden voor verwijderen",
                tool_id=id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")
        log.infox(
            "Tool verwijderen afgerond",
            tool_id=id,
            success=success,
        )
        return {"detail": "Tool deleted"}

    def get_all_for_server(self, mcp_server_id: int, only_enabled: bool = False):
        log.infox(
            "Tools voor MCP server ophalen gestart",
            mcp_server_id=mcp_server_id,
            only_enabled=only_enabled,
        )
        result = self.repository.get_all_for_server(mcp_server_id=mcp_server_id, only_enabled=only_enabled)
        log.infox(
            "Tools voor MCP server ophalen afgerond",
            mcp_server_id=mcp_server_id,
            only_enabled=only_enabled,
            count=len(result) if result is not None else None,
        )
        return result