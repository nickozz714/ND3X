"""Internal endpoint the Claude Code MCP gateway calls to execute a tool.

The gateway subprocess delegates tool execution here (see services/mcp/
internal_auth.py for why) so stdio-backed tools (Fabric/OneLake) and their Azure
session run once, in the main server process. Auth is the in-process shared
secret — NOT user JWT — because the caller is our own gateway child, not a user.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from component.logging import get_logger
from db.database import get_db
from services.mcp.internal_auth import verify_internal_token

log = get_logger(__name__)

router = APIRouter(prefix="/internal/mcp", tags=["internal"])


class InternalToolExecuteRequest(BaseModel):
    tool_id: int
    args: Dict[str, Any] = {}


def require_internal_token(x_nd3x_internal_token: Optional[str] = Header(default=None)) -> None:
    if not verify_internal_token(x_nd3x_internal_token):
        log.warningx("Interne MCP-execute geweigerd: ongeldig token")
        raise HTTPException(status_code=401, detail="invalid internal token")


@router.post("/execute", dependencies=[Depends(require_internal_token)])
async def internal_mcp_execute(
    payload: InternalToolExecuteRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Execute one enabled tool by id in the main-server runtime and return its
    result. Mirrors what the gateway would have done locally, but here the stdio
    process manager + Azure session are actually available."""
    from services.mcp.tool_execution_service import ToolExecutionService

    log.infox("Interne MCP-execute", tool_id=payload.tool_id, arg_keys=list((payload.args or {}).keys()))
    result = await ToolExecutionService(db).execute_tool(payload.tool_id, payload.args or {})
    return {"ok": True, "result": result}
