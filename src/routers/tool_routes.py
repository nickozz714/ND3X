from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from authentication.dependencies import require_admin_user
from db.database import get_db
from schemas.tool import ToolCreate, ToolResponse, ToolUpdate, ToolWithRelations
from services.assistants.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["Tool"])


class _EnabledIn(BaseModel):
    enabled: bool


def get_service(db: Session = Depends(get_db)) -> ToolService:
    return ToolService(db)


@router.get("", response_model=list[ToolResponse])
def get_all(
    skip: int = 0,
    limit: int = 100,
    service: ToolService = Depends(get_service),
):
    return service.get_all(skip=skip, limit=limit)


@router.get("/full", response_model=list[ToolWithRelations])
def get_all_with_relations(
    skip: int = 0,
    limit: int = 100,
    service: ToolService = Depends(get_service),
):
    return service.get_all_with_relations(skip=skip, limit=limit)


# Capability tools that delegate to an agent / spawn background work — these don't make
# sense as a deterministic workflow step (the workflow IS the orchestration), so they're
# not offered as selectable "tool" operations.
_NON_OPERATION_TOOLS = {
    "agent__dispatch",
    "task__create", "task__status", "task__result", "task__list",
}


@router.get("/builtin")
def list_builtin_tools():
    """Builtin tools that can be run directly as a workflow 'tool' operation (name +
    description + argument schema). In-process data tools only — agent/background
    capability tools are excluded (a workflow doesn't need a sub-agent)."""
    from services.builtin.internal_tool_registry import internal_tool_registry
    return [
        {
            "name": t.get("name"),
            "title": t.get("title"),
            "description": t.get("description"),
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
        }
        for t in internal_tool_registry.list_tools()
        if t.get("name") not in _NON_OPERATION_TOOLS
    ]


@router.get("/builtin-server", response_model=list[ToolResponse])
def list_builtin_server_tools(db: Session = Depends(get_db)):
    """The DB rows for the always-on Builtin tool set (id + is_enabled), for the
    admin enable/disable panel. Empty if the Builtin server isn't present."""
    from models.mcp_server import MCPServer
    server = db.query(MCPServer).filter(MCPServer.name == "Builtin").first()
    if not server:
        return []
    return ToolService(db).get_all_for_server(mcp_server_id=server.id)


@router.post("/{tool_id}/enabled", response_model=ToolResponse, dependencies=[Depends(require_admin_user)])
def set_tool_enabled(tool_id: int, body: _EnabledIn, service: ToolService = Depends(get_service)):
    """Admin: turn a tool (e.g. a builtin like system__shell_exec) on/off. Disabling
    removes it from every chat; the boot sync preserves the choice."""
    return service.update(tool_id, ToolUpdate(is_enabled=body.enabled))


@router.get("/{tool_id}", response_model=ToolResponse)
def get_by_id(
    tool_id: int,
    service: ToolService = Depends(get_service),
):
    return service.get_by_id(tool_id)


@router.get("/{tool_id}/full", response_model=ToolWithRelations)
def get_with_relations(
    tool_id: int,
    service: ToolService = Depends(get_service),
):
    return service.get_with_relations(tool_id)


@router.post("", response_model=ToolResponse, status_code=201)
def create(
    data: ToolCreate,
    service: ToolService = Depends(get_service),
):
    return service.create(data)


@router.put("/{tool_id}", response_model=ToolResponse)
def update(
    tool_id: int,
    data: ToolUpdate,
    service: ToolService = Depends(get_service),
):
    return service.update(tool_id, data)


@router.delete("/{tool_id}")
def delete(
    tool_id: int,
    service: ToolService = Depends(get_service),
):
    return service.delete(tool_id)



@router.get("/server/{mcp_server_id}", response_model=list[ToolResponse])
def get_tools_for_server(
    mcp_server_id: int,
    only_enabled: bool = False,
    service: ToolService = Depends(get_service),
):
    return service.get_all_for_server(mcp_server_id=mcp_server_id, only_enabled=only_enabled)