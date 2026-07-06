import logging
from sqlalchemy.orm import Session, joinedload
from models.assistant import Assistant
from models.tool import Tool
from typing import Optional

logger = logging.getLogger(__name__)


class AssistantToolRepository:
    def __init__(self, db: Session):
        self.db = db

    def add_tool_to_assistant(self, assistant_id: int, tool_id: int) -> Optional[Assistant]:
        logger.info("Linking tool id=%s to assistant id=%s", tool_id, assistant_id)

        assistant = (
            self.db.query(Assistant)
            .options(joinedload(Assistant.tools).joinedload(Tool.mcp_server))
            .filter(Assistant.id == assistant_id)
            .first()
        )
        tool = (
            self.db.query(Tool)
            .options(joinedload(Tool.mcp_server))
            .filter(Tool.id == tool_id)
            .first()
        )

        if not assistant or not tool:
            logger.warning("Assistant or Tool not found (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
            return None

        if tool not in assistant.tools:
            assistant.tools.append(tool)
            self.db.commit()
            self.db.refresh(assistant)

        return assistant

    def remove_tool_from_assistant(self, assistant_id: int, tool_id: int) -> Optional[Assistant]:
        logger.info("Unlinking tool id=%s from assistant id=%s", tool_id, assistant_id)

        assistant = (
            self.db.query(Assistant)
            .options(joinedload(Assistant.tools).joinedload(Tool.mcp_server))
            .filter(Assistant.id == assistant_id)
            .first()
        )
        tool = (
            self.db.query(Tool)
            .options(joinedload(Tool.mcp_server))
            .filter(Tool.id == tool_id)
            .first()
        )

        if not assistant or not tool:
            logger.warning("Assistant or Tool not found (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
            return None

        if tool in assistant.tools:
            assistant.tools.remove(tool)
            self.db.commit()
            self.db.refresh(assistant)

        return assistant

    def get_tools_for_assistant(self, assistant_id: int) -> list[Tool]:
        logger.debug("Fetching tools for assistant id=%s", assistant_id)

        assistant = (
            self.db.query(Assistant)
            .options(joinedload(Assistant.tools).joinedload(Tool.mcp_server))
            .filter(Assistant.id == assistant_id)
            .first()
        )
        if not assistant:
            logger.warning("Assistant not found id=%s", assistant_id)
            return []

        return assistant.tools

    def get_assistants_for_tool(self, tool_id: int) -> list[Assistant]:
        logger.debug("Fetching assistants for tool id=%s", tool_id)

        tool = (
            self.db.query(Tool)
            .options(joinedload(Tool.mcp_server))
            .filter(Tool.id == tool_id)
            .first()
        )
        if not tool:
            logger.warning("Tool not found id=%s", tool_id)
            return []

        return tool.assistants