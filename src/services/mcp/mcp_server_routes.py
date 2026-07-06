from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from db.database import get_db
from services.mcp.mcp_server_service import MCPServerService
from services.mcp.mcp_server_auth_service import MCPServerAuthService
from services.mcp.mcp_server_sync_service import MCPServerSyncService
from services.assistants.ask_job_callbacks import stdio_process_manager

from schemas.mcp_server import (
    MCPServerCreate,
    MCPServerUpdate,
    MCPServerResponse,
    MCPServerWithRelations,
)
from schemas.mcp_server_auth import MCPServerAuthUpsert, MCPServerAuthResponse

router = APIRouter(prefix="/mcp-servers", tags=["MCP Server"])


def get_server_service(db: Session = Depends(get_db)) -> MCPServerService:
    return MCPServerService(db)


def get_auth_service(db: Session = Depends(get_db)) -> MCPServerAuthService:
    return MCPServerAuthService(db)


def get_sync_service(db: Session = Depends(get_db)) -> MCPServerSyncService:
    return MCPServerSyncService(db)


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MCPServerResponse])
def get_all(
    skip: int = 0,
    limit: int = 100,
    service: MCPServerService = Depends(get_server_service),
):
    return service.get_all(skip=skip, limit=limit)


@router.get("/{mcp_server_id}", response_model=MCPServerResponse)
def get_by_id(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    return service.get_by_id(mcp_server_id)


@router.get("/{mcp_server_id}/full", response_model=MCPServerWithRelations)
def get_with_relations(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    return service.get_with_relations(mcp_server_id)


@router.post("", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED)
async def create(
    data: MCPServerCreate,
    service: MCPServerService = Depends(get_server_service),
):
    server = service.create(data)

    # Start stdio server direct na aanmaken
    if server.server_type == "stdio" and server.stdio_command and server.is_enabled:
        await stdio_process_manager.start_server(
            name=server.slug,
            command=server.stdio_command,
        )

    return server


@router.put("/{mcp_server_id}", response_model=MCPServerResponse)
async def update(
    mcp_server_id: int,
    data: MCPServerUpdate,
    service: MCPServerService = Depends(get_server_service),
):
    server = service.update(mcp_server_id, data)

    # Herstart stdio server als command of enabled status gewijzigd is
    if server.server_type == "stdio":
        if server.is_enabled and server.stdio_command:
            await stdio_process_manager.restart_server(
                name=server.slug,
                command=server.stdio_command,
            )
        else:
            await stdio_process_manager.stop_server(name=server.slug)

    return server


@router.delete("/{mcp_server_id}")
async def delete(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    server = service.get_by_id(mcp_server_id)

    # Stop stdio server voor verwijderen
    if server.server_type == "stdio":
        await stdio_process_manager.stop_server(name=server.slug)

    return service.delete(mcp_server_id)


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.get("/{mcp_server_id}/auth/active", response_model=MCPServerAuthResponse)
def get_active_auth(
    mcp_server_id: int,
    service: MCPServerAuthService = Depends(get_auth_service),
):
    return service.get_active_for_server(mcp_server_id)


@router.put("/{mcp_server_id}/auth/active", response_model=MCPServerAuthResponse)
def upsert_active_auth(
    mcp_server_id: int,
    data: MCPServerAuthUpsert,
    service: MCPServerAuthService = Depends(get_auth_service),
):
    return service.upsert_active_for_server(mcp_server_id, data)


# ── Sync tools ────────────────────────────────────────────────────────────────

@router.post("/{mcp_server_id}/sync-tools", response_model=MCPServerWithRelations)
async def sync_tools(
    mcp_server_id: int,
    service: MCPServerSyncService = Depends(get_sync_service),
):
    return await service.sync_server_tools(mcp_server_id)


# ── Stdio lifecycle ───────────────────────────────────────────────────────────

@router.post("/{mcp_server_id}/start", response_model=MCPServerResponse)
async def start_stdio_server(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    """Start een gestopte stdio server handmatig opnieuw."""
    server = service.get_by_id(mcp_server_id)
    if server.server_type != "stdio":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Alleen stdio servers kunnen handmatig gestart worden")
    if not server.stdio_command:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Server heeft geen stdio_command geconfigureerd")

    await stdio_process_manager.start_server(name=server.slug, command=server.stdio_command)
    return server


@router.post("/{mcp_server_id}/stop", response_model=MCPServerResponse)
async def stop_stdio_server(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    """Stop een draaiende stdio server handmatig."""
    server = service.get_by_id(mcp_server_id)
    if server.server_type != "stdio":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Alleen stdio servers kunnen handmatig gestopt worden")

    await stdio_process_manager.stop_server(name=server.slug)
    return server


@router.post("/{mcp_server_id}/restart", response_model=MCPServerResponse)
async def restart_stdio_server(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    """Herstart een stdio server — handig na een update of crash."""
    server = service.get_by_id(mcp_server_id)
    if server.server_type != "stdio":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Alleen stdio servers kunnen herstart worden")
    if not server.stdio_command:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Server heeft geen stdio_command geconfigureerd")

    await stdio_process_manager.restart_server(name=server.slug, command=server.stdio_command)
    return server


@router.get("/{mcp_server_id}/status")
def get_stdio_status(
    mcp_server_id: int,
    service: MCPServerService = Depends(get_server_service),
):
    """Geeft terug of een stdio server actief draait als child process."""
    server = service.get_by_id(mcp_server_id)
    return {
        "mcp_server_id": mcp_server_id,
        "slug": server.slug,
        "server_type": server.server_type,
        "running": stdio_process_manager.is_running(server.slug) if server.server_type == "stdio" else None,
    }
