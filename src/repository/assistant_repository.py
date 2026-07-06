import logging
from sqlalchemy.orm import Session, joinedload
from models.assistant import Assistant
from models.tool import Tool
from schemas.assistant import AssistantCreate, AssistantUpdate
from typing import Optional

logger = logging.getLogger(__name__)


class AssistantRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, skip: int = 0, limit: int = 100) -> list[Assistant]:
        logger.debug("Fetching all assistant (skip=%d, limit=%d)", skip, limit)
        return self.db.query(Assistant).offset(skip).limit(limit).all()

    def get_by_id(self, id: int) -> Optional[Assistant]:
        logger.debug("Fetching assistant by id=%s", id)
        return self.db.query(Assistant).filter(Assistant.id == id).first()

    def get_by_name(self, name: str) -> Optional[Assistant]:
        logger.debug("Fetching assistant by name=%s", name)
        return self.db.query(Assistant).filter(Assistant.name == name).first()

    def get_with_relations(self, id: int) -> Optional[Assistant]:
        logger.debug("Fetching assistant with relations by id=%s", id)
        query = self.db.query(Assistant)
        query = query.options(
            joinedload(Assistant.tools).joinedload(Tool.mcp_server)
        )
        return query.filter(Assistant.id == id).first()

    def get_all_with_relations(self, skip: int = 0, limit: int = 100) -> list[Assistant]:
        logger.debug("Fetching all assistant with relations (skip=%d, limit=%d)", skip, limit)
        query = self.db.query(Assistant)
        query = query.options(
            joinedload(Assistant.tools).joinedload(Tool.mcp_server)
        )
        return query.offset(skip).limit(limit).all()

    def create(self, data: AssistantCreate) -> Assistant:
        logger.info("Creating new assistant")
        db_obj = Assistant(**data.model_dump(exclude_unset=True))
        self.db.add(db_obj)
        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Created assistant with id=%s", db_obj.id)
        return db_obj

    def update(self, id: int, data: AssistantUpdate) -> Optional[Assistant]:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("Assistant with id=%s not found for update", id)
            return None
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_obj, key, value)
        self.db.commit()
        self.db.refresh(db_obj)
        logger.info("Updated assistant id=%s", id)
        return db_obj

    def delete(self, id: int) -> bool:
        db_obj = self.get_by_id(id)
        if not db_obj:
            logger.warning("Assistant with id=%s not found for delete", id)
            return False
        self.db.delete(db_obj)
        self.db.commit()
        logger.info("Deleted assistant id=%s", id)
        return True