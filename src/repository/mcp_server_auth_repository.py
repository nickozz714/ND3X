import logging
from datetime import datetime

from sqlalchemy.orm import Session
from typing import Optional

from models.mcp_server import MCPServerAuth

logger = logging.getLogger(__name__)


class MCPServerAuthRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all_for_server(self, mcp_server_id: int) -> list[MCPServerAuth]:
        logger.debug("Fetching auth configs for mcp_server_id=%s", mcp_server_id)
        return (
            self.db.query(MCPServerAuth)
            .filter(MCPServerAuth.mcp_server_id == mcp_server_id)
            .all()
        )

    def get_active_for_server(self, mcp_server_id: int) -> Optional[MCPServerAuth]:
        logger.debug("Fetching active auth config for mcp_server_id=%s", mcp_server_id)
        return (
            self.db.query(MCPServerAuth)
            .filter(
                MCPServerAuth.mcp_server_id == mcp_server_id,
                MCPServerAuth.is_active == True,
            )
            .first()
        )

    def get_by_id(self, id: int) -> Optional[MCPServerAuth]:
        logger.debug("Fetching mcp auth by id=%s", id)
        return self.db.query(MCPServerAuth).filter(MCPServerAuth.id == id).first()

    def create(self, data):
        logger.info("Creating mcp auth config")
        db_obj = MCPServerAuth(**data.model_dump(exclude_unset=True))
        self.db.add(db_obj)
        self.db.commit()
        self.db.refresh(db_obj)
        return db_obj

    def update(self, id: int, data):
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("MCPServerAuth with id=%s not found for update", id)
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_obj, key, value)

        self.db.commit()
        self.db.refresh(db_obj)
        return db_obj

    def upsert_active_for_server(self, mcp_server_id: int, data):
        current = self.get_active_for_server(mcp_server_id)

        if current:
            update_data = data.model_dump(exclude_unset=True)
            for key, value in update_data.items():
                setattr(current, key, value)
            current.mcp_server_id = mcp_server_id
            current.is_active = True
            self.db.commit()
            self.db.refresh(current)
            return current

        payload = data.model_dump(exclude_unset=True)
        payload["mcp_server_id"] = mcp_server_id
        payload["is_active"] = True
        db_obj = MCPServerAuth(**payload)
        db_obj.created_at = datetime.utcnow()
        db_obj.updated_at = datetime.utcnow()
        self.db.add(db_obj)
        self.db.commit()
        self.db.refresh(db_obj)
        return db_obj

    def deactivate_others_for_server(self, mcp_server_id: int, keep_id: int):
        (
            self.db.query(MCPServerAuth)
            .filter(
                MCPServerAuth.mcp_server_id == mcp_server_id,
                MCPServerAuth.id != keep_id,
                MCPServerAuth.is_active == True,
            )
            .update({"is_active": False}, synchronize_session=False)
        )
        self.db.commit()

    def delete(self, id: int) -> bool:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("MCPServerAuth with id=%s not found for delete", id)
            return False

        self.db.delete(db_obj)
        self.db.commit()
        return True