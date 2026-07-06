import logging
from sqlalchemy.orm import Session, joinedload
from models.tool import Tool
from models.assistant_tool import assistant_tool
from models.skill_tool import SkillTool
from schemas.tool import ToolCreate, ToolUpdate
from typing import Optional

logger = logging.getLogger(__name__)


class ToolRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, skip: int = 0, limit: int = 100) -> list[Tool]:
        logger.debug("Fetching all tool (skip=%d, limit=%d)", skip, limit)
        return self.db.query(Tool).offset(skip).limit(limit).all()

    def get_by_id(self, id: int) -> Optional[Tool]:
        logger.debug("Fetching tool by id=%s", id)
        return self.db.query(Tool).filter(Tool.id == id).first()

    def get_with_relations(self, id: int) -> Optional[Tool]:
        logger.debug("Fetching tool with relations by id=%s", id)
        query = self.db.query(Tool)
        query = query.options(joinedload(Tool.assistants))
        return query.filter(Tool.id == id).first()

    def get_all_with_relations(self, skip: int = 0, limit: int = 100) -> list[Tool]:
        logger.debug("Fetching all tool with relations (skip=%d, limit=%d)", skip, limit)
        query = self.db.query(Tool)
        query = query.options(joinedload(Tool.assistants))
        return query.offset(skip).limit(limit).all()

    def create(self, data: ToolCreate) -> Tool:
        logger.info("Creating new tool")
        db_obj = Tool(**data.model_dump(exclude_unset=True))
        self.db.add(db_obj)
        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Created tool with id=%s", db_obj.id)
        return db_obj

    def update(self, id: int, data: ToolUpdate) -> Optional[Tool]:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("Tool with id=%s not found for update", id)
            return None
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_obj, key, value)
        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Updated tool id=%s", id)
        return db_obj

    def delete(self, id: int) -> bool:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("Tool with id=%s not found for delete", id)
            return False
        # Cascade-remove link rows explicitly in the same transaction. We cannot
        # rely on the DB FK `ondelete=CASCADE` alone because SQLite only enforces
        # it with `PRAGMA foreign_keys=ON`, and there is no ORM relationship from
        # Tool -> SkillTool to let the ORM clean those rows. Guarding here means a
        # deleted tool never leaves a dangling skill_tool / assistant_tool row.
        skill_links = self.db.query(SkillTool).filter(SkillTool.tool_id == id).delete(
            synchronize_session=False
        )
        assistant_links = self.db.execute(
            assistant_tool.delete().where(assistant_tool.c.tool_id == id)
        ).rowcount
        self.db.delete(db_obj)
        self.db.commit()
        logger.info(
            "Deleted tool id=%s (removed %s skill_tool, %s assistant_tool links)",
            id,
            skill_links,
            assistant_links,
        )
        return True

    def get_with_server(self, id: int) -> Optional[Tool]:
        logger.debug("Fetching tool with server by id=%s", id)
        query = self.db.query(Tool)
        query = query.options(
            joinedload(Tool.assistants),
            joinedload(Tool.mcp_server),
        )
        return query.filter(Tool.id == id).first()

    def get_all_for_server(self, mcp_server_id: int, only_enabled: bool = False) -> list[Tool]:
        logger.debug("Fetching tools for mcp_server_id=%s", mcp_server_id)
        query = self.db.query(Tool).filter(Tool.mcp_server_id == mcp_server_id)
        if only_enabled:
            query = query.filter(Tool.is_enabled == True)
        return query.all()

    def get_by_server_and_remote_name(self, mcp_server_id: int, remote_name: str) -> Optional[Tool]:
        logger.debug(
            "Fetching tool by mcp_server_id=%s and remote_name=%s",
            mcp_server_id,
            remote_name,
        )
        return (
            self.db.query(Tool)
            .filter(
                Tool.mcp_server_id == mcp_server_id,
                Tool.remote_name == remote_name,
            )
            .first()
        )

    def get_by_server_and_name(self, mcp_server_id: int, name: str) -> Optional[Tool]:
        logger.debug(
            "Fetching tool by mcp_server_id=%s and name=%s",
            mcp_server_id,
            name,
        )
        return (
            self.db.query(Tool)
            .filter(
                Tool.mcp_server_id == mcp_server_id,
                Tool.name == name,
            )
            .first()
        )

    def get_all_with_relations(self, skip: int = 0, limit: int = 100) -> list[Tool]:
        logger.debug("Fetching all tool with relations (skip=%d, limit=%d)", skip, limit)
        query = self.db.query(Tool)
        query = query.options(
            joinedload(Tool.assistants),
            joinedload(Tool.mcp_server),
        )
        return query.offset(skip).limit(limit).all()

    def get_with_relations(self, id: int) -> Optional[Tool]:
        logger.debug("Fetching tool with relations by id=%s", id)
        query = self.db.query(Tool)
        query = query.options(
            joinedload(Tool.assistants),
            joinedload(Tool.mcp_server),
        )
        return query.filter(Tool.id == id).first()
