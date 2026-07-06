from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException

from component.logging import get_logger
from repository.mcp_server_repository import MCPServerRepository
from repository.mcp_server_auth_repository import MCPServerAuthRepository
from repository.tool_repository import ToolRepository
from services.mcp.mcp_client_factory import MCPClientFactory
from services.assistants.ask_job_callbacks import stdio_process_manager, builtin_mcp_client


log = get_logger(__name__)


class MCPServerSyncService:
    def __init__(self, db: Session):
        log.debugx(
            "MCPServerSyncService initialiseren",
            has_db_session=db is not None,
        )
        self.db = db
        self.server_repo = MCPServerRepository(db)
        log.debugx("MCPServerRepository gekoppeld aan MCPServerSyncService")
        self.auth_repo = MCPServerAuthRepository(db)
        log.debugx("MCPServerAuthRepository gekoppeld aan MCPServerSyncService")
        self.tool_repo = ToolRepository(db)
        log.debugx("ToolRepository gekoppeld aan MCPServerSyncService")
        self.client_factory = MCPClientFactory(
            stdio_process_manager=stdio_process_manager,
            builtin_mcp_client=builtin_mcp_client,
        )
        log.debugx("MCPClientFactory gekoppeld aan MCPServerSyncService")

    async def sync_server_tools(self, mcp_server_id: int):
        log.infox(
            "MCP server tools sync gestart",
            mcp_server_id=mcp_server_id,
        )
        server = self.server_repo.get_by_id(mcp_server_id)
        if not server:
            log.warningx(
                "MCP server tools sync mislukt: MCP server niet gevonden",
                mcp_server_id=mcp_server_id,
            )
            raise HTTPException(status_code=404, detail="MCP server not found")

        log.debugx(
            "MCP server gevonden voor tools sync",
            mcp_server_id=mcp_server_id,
            server_id=getattr(server, "id", None),
            server_name=getattr(server, "name", None),
            base_url=getattr(server, "base_url", None),
            server_type=getattr(server, "server_type", None),
            is_enabled=getattr(server, "is_enabled", None),
        )

        if not server.is_enabled:
            log.warningx(
                "MCP server tools sync afgebroken: server is uitgeschakeld",
                mcp_server_id=mcp_server_id,
                server_name=getattr(server, "name", None),
            )
            raise HTTPException(status_code=400, detail="MCP server is disabled")

        auth = self.auth_repo.get_active_for_server(mcp_server_id)
        log.debugx(
            "Actieve MCP auth opgehaald voor tools sync",
            mcp_server_id=mcp_server_id,
            has_auth=auth is not None,
            auth_id=getattr(auth, "id", None) if auth else None,
            auth_type=getattr(auth, "auth_type", None) if auth else None,
        )

        client = self.client_factory.build(server=server, auth=auth)
        log.debugx(
            "MCP client gebouwd voor tools sync",
            mcp_server_id=mcp_server_id,
            server_name=getattr(server, "name", None),
            server_type=getattr(server, "server_type", None),
        )

        try:
            log.infox(
                "Remote MCP tools listing ophalen gestart",
                mcp_server_id=mcp_server_id,
                server_name=getattr(server, "name", None),
            )
            listing = await client.list_tools_listing()
            remote_tools = listing.get("tools", [])
            seen_remote_names = set()

            log.infox(
                "Remote MCP tools listing opgehaald",
                mcp_server_id=mcp_server_id,
                remote_tool_count=len(remote_tools) if isinstance(remote_tools, list) else None,
            )

            for item in remote_tools:
                remote_name = item["name"]
                seen_remote_names.add(remote_name)

                log.debugx(
                    "Remote MCP tool verwerken",
                    mcp_server_id=mcp_server_id,
                    remote_name=remote_name,
                    has_description=bool(item.get("description")),
                    has_input_schema=bool(item.get("inputSchema")),
                )

                existing = self.tool_repo.get_by_server_and_remote_name(
                    mcp_server_id=mcp_server_id,
                    remote_name=remote_name,
                )

                payload = {
                    "mcp_server_id": mcp_server_id,
                    "name": remote_name,
                    "remote_name": remote_name,
                    "description": item.get("description") or "",
                    "argument": item.get("inputSchema") or {},
                    "output_schema": item.get("outputSchema"),
                    "annotations": item.get("annotations") or {},
                    "meta": item.get("meta") or {},
                    "type": "mcp",
                    "tool_instructions": "",
                    "is_enabled": True,
                }

                if existing:
                    payload["updated_at"] = datetime.now(timezone.utc)
                    # Preserve an admin's enable/disable choice across re-syncs (incl.
                    # the boot sync) — only set is_enabled when first creating the tool.
                    payload.pop("is_enabled", None)
                    log.infox(
                        "Lokale MCP tool bijwerken",
                        mcp_server_id=mcp_server_id,
                        tool_id=getattr(existing, "id", None),
                        remote_name=remote_name,
                    )
                    self.tool_repo.update(existing.id, type("Obj", (), {"model_dump": lambda self, exclude_unset=True: payload})())
                else:
                    payload["created_at"] = datetime.now(timezone.utc)
                    payload["updated_at"] = datetime.now(timezone.utc)
                    log.infox(
                        "Nieuwe lokale MCP tool aanmaken",
                        mcp_server_id=mcp_server_id,
                        remote_name=remote_name,
                    )
                    self.tool_repo.create(type("Obj", (), {"model_dump": lambda self, exclude_unset=True: payload})())

            db_tools = self.tool_repo.get_all_for_server(mcp_server_id)
            removed_tool_count = 0
            for db_tool in db_tools:
                remote_name = getattr(db_tool, "remote_name", None) or db_tool.name
                if remote_name not in seen_remote_names:
                    # Tool no longer exists on the server → delete it from the DB and
                    # cascade-remove its skill_tool / assistant_tool links (tool_repo.delete
                    # cleans those) so skills don't keep dangling references.
                    log.warningx(
                        "Lokale MCP tool niet meer remote aanwezig, wordt verwijderd (incl. skill/assistant-links)",
                        mcp_server_id=mcp_server_id,
                        tool_id=getattr(db_tool, "id", None),
                        remote_name=remote_name,
                    )
                    self.tool_repo.delete(db_tool.id)
                    removed_tool_count += 1

            server.last_synced_at = datetime.now(timezone.utc)
            server.last_sync_status = "success"
            server.last_sync_error = None
            self.db.commit()

            log.infox(
                "MCP server tools sync afgerond",
                mcp_server_id=mcp_server_id,
                remote_tool_count=len(remote_tools) if isinstance(remote_tools, list) else None,
                removed_tool_count=removed_tool_count,
            )
            return self.server_repo.get_with_relations(mcp_server_id)

        except Exception as e:
            log.errorx(
                "MCP server tools sync mislukt",
                mcp_server_id=mcp_server_id,
                server_name=getattr(server, "name", None),
                error=str(e),
            )
            server.last_synced_at = datetime.now(timezone.utc)
            server.last_sync_status = "failed"
            server.last_sync_error = str(e)
            self.db.commit()
            raise
