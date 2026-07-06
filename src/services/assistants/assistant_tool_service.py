import logging
from sqlalchemy.orm import Session
from repository.assistant_tool_repository import AssistantToolRepository
from fastapi import HTTPException
from component.logging import get_logger

logger = logging.getLogger(__name__)
log = get_logger(__name__)


class AssistantToolService:
    def __init__(self, db: Session):
        log.debugx(
            "AssistantToolService initialiseren",
            has_db_session=db is not None,
        )
        self.repository = AssistantToolRepository(db)
        log.debugx("AssistantToolRepository gekoppeld aan AssistantToolService")

    def add_tool_to_assistant(self, assistant_id: int, tool_id: int):
        logger.info("Service: add tool to assistant (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
        log.infox(
            "Tool koppelen aan assistant gestart",
            assistant_id=assistant_id,
            tool_id=tool_id,
        )

        obj = self.repository.add_tool_to_assistant(assistant_id, tool_id)
        if not obj:
            logger.warning("Assistant or Tool not found (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
            log.warningx(
                "Tool koppelen aan assistant mislukt: assistant of tool niet gevonden",
                assistant_id=assistant_id,
                tool_id=tool_id,
            )
            raise HTTPException(status_code=404, detail="Assistant or Tool not found")

        log.infox(
            "Tool koppelen aan assistant afgerond",
            assistant_id=assistant_id,
            tool_id=tool_id,
            result_id=getattr(obj, "id", None),
        )
        return obj

    def remove_tool_from_assistant(self, assistant_id: int, tool_id: int):
        logger.info("Service: remove tool from assistant (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
        log.infox(
            "Tool loskoppelen van assistant gestart",
            assistant_id=assistant_id,
            tool_id=tool_id,
        )

        obj = self.repository.remove_tool_from_assistant(assistant_id, tool_id)
        if not obj:
            logger.warning("Assistant or Tool not found (assistant_id=%s, tool_id=%s)", assistant_id, tool_id)
            log.warningx(
                "Tool loskoppelen van assistant mislukt: assistant of tool niet gevonden",
                assistant_id=assistant_id,
                tool_id=tool_id,
            )
            raise HTTPException(status_code=404, detail="Assistant or Tool not found")

        log.infox(
            "Tool loskoppelen van assistant afgerond",
            assistant_id=assistant_id,
            tool_id=tool_id,
            result_id=getattr(obj, "id", None),
        )
        return obj

    def get_tools_for_assistant(self, assistant_id: int):
        logger.debug("Service: get tools for assistant id=%s", assistant_id)
        log.infox(
            "Tools voor assistant ophalen gestart",
            assistant_id=assistant_id,
        )

        tools = self.repository.get_tools_for_assistant(assistant_id)
        if tools is None:
            logger.warning("Assistant not found: id=%s", assistant_id)
            log.warningx(
                "Tools voor assistant ophalen mislukt: assistant niet gevonden",
                assistant_id=assistant_id,
            )
            raise HTTPException(status_code=404, detail="Assistant not found")

        log.infox(
            "Tools voor assistant ophalen afgerond",
            assistant_id=assistant_id,
            count=len(tools) if tools is not None else None,
        )
        return tools

    def get_assistants_for_tool(self, tool_id: int):
        logger.debug("Service: get assistants for tool id=%s", tool_id)
        log.infox(
            "Assistants voor tool ophalen gestart",
            tool_id=tool_id,
        )

        assistants = self.repository.get_assistants_for_tool(tool_id)
        if assistants is None:
            logger.warning("Tool not found: id=%s", tool_id)
            log.warningx(
                "Assistants voor tool ophalen mislukt: tool niet gevonden",
                tool_id=tool_id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")

        log.infox(
            "Assistants voor tool ophalen afgerond",
            tool_id=tool_id,
            count=len(assistants) if assistants is not None else None,
        )
        return assistants