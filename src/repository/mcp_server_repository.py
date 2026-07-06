import logging
from datetime import datetime

from sqlalchemy.orm import Session, joinedload
from typing import Optional

from models.mcp_server import MCPServer

logger = logging.getLogger(__name__)


class MCPServerRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, skip: int = 0, limit: int = 100) -> list[MCPServer]:
        logger.debug("Fetching all mcp servers (skip=%d, limit=%d)", skip, limit)
        return self.db.query(MCPServer).offset(skip).limit(limit).all()

    def get_enabled(self) -> list[MCPServer]:
        logger.debug("Fetching enabled mcp servers")
        return self.db.query(MCPServer).filter(MCPServer.is_enabled == True).all()

    def get_by_id(self, id: int) -> Optional[MCPServer]:
        logger.debug("Fetching mcp server by id=%s", id)
        return self.db.query(MCPServer).filter(MCPServer.id == id).first()

    def get_by_name(self, name: str) -> Optional[MCPServer]:
        logger.debug("Fetching mcp server by name=%s", name)
        return self.db.query(MCPServer).filter(MCPServer.name == name).first()

    def get_by_slug(self, slug: str) -> Optional[MCPServer]:
        logger.debug("Fetching mcp server by slug=%s", slug)
        return self.db.query(MCPServer).filter(MCPServer.slug == slug).first()

    def get_with_relations(self, id: int) -> Optional[MCPServer]:
        logger.debug("Fetching mcp server with relations by id=%s", id)
        query = self.db.query(MCPServer)
        query = query.options(
            joinedload(MCPServer.tools),
            joinedload(MCPServer.auth_configs),
        )
        return query.filter(MCPServer.id == id).first()

    def create(self, data):
        logger.info("Creating new mcp server")
        db_obj = MCPServer(**data.model_dump(exclude_unset=True))
        db_obj.created_at = datetime.utcnow()
        db_obj.updated_at = datetime.utcnow()
        self.db.add(db_obj)
        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Created mcp server with id=%s", db_obj.id)
        return db_obj

    def update(self, id: int, data):
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("MCPServer with id=%s not found for update", id)
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_obj, key, value)

        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Updated mcp server id=%s", id)
        return db_obj

    def delete(self, id: int) -> bool:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("MCPServer with id=%s not found for delete", id)
            return False

        self.db.delete(db_obj)
        self.db.commit()
        logger.info("Deleted mcp server id=%s", id)
        return True