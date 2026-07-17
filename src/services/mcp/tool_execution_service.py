from sqlalchemy.orm import Session
from fastapi import HTTPException

from component.logging import get_logger
from repository.tool_repository import ToolRepository
from repository.mcp_server_auth_repository import MCPServerAuthRepository
from services.mcp.mcp_client_factory import MCPClientFactory


log = get_logger(__name__)


class ToolExecutionService:
    def __init__(self, db: Session):
        log.debugx(
            "ToolExecutionService initialiseren",
            has_db_session=db is not None,
        )
        self.tool_repo = ToolRepository(db)
        log.debugx("ToolRepository gekoppeld aan ToolExecutionService")
        self.auth_repo = MCPServerAuthRepository(db)
        log.debugx("MCPServerAuthRepository gekoppeld aan ToolExecutionService")
        self.client_factory = MCPClientFactory()
        # Attach the shared builtin MCP client AND the shared stdio process manager
        # so builtin (in-process) tools — text search/ingest, internal tools,
        # shell/az-login — AND stdio MCP servers (Fabric/OneLake, …) both resolve.
        # Without the stdio manager, building a client for a `stdio` server raises
        # "StdioProcessManager is niet geconfigureerd". Lazy import avoids a
        # circular import with ask_job_callbacks (which imports this module).
        try:
            from services.assistants.ask_job_callbacks import builtin_mcp_client
            if builtin_mcp_client is not None:
                self.client_factory.set_builtin_mcp_client(builtin_mcp_client)
        except Exception as exc:  # noqa: BLE001 — never break construction on wiring
            log.warningx("Builtin MCP client niet gekoppeld aan ToolExecutionService", error=str(exc))
        try:
            from services.assistants.ask_job_callbacks import stdio_process_manager
            if stdio_process_manager is not None:
                self.client_factory.set_stdio_process_manager(stdio_process_manager)
        except Exception as exc:  # noqa: BLE001 — never break construction on wiring
            log.warningx("Stdio process manager niet gekoppeld aan ToolExecutionService", error=str(exc))
        log.debugx("MCPClientFactory gekoppeld aan ToolExecutionService")

    async def execute_tool(self, tool_id: int, args: dict):
        log.infox(
            "Tool uitvoering gestart",
            tool_id=tool_id,
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
        )

        tool = self.tool_repo.get_with_server(tool_id)
        if not tool:
            log.warningx(
                "Tool uitvoering mislukt: tool niet gevonden",
                tool_id=tool_id,
            )
            raise HTTPException(status_code=404, detail="Tool not found")

        log.debugx(
            "Tool gevonden voor uitvoering",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            remote_name=getattr(tool, "remote_name", None),
            is_enabled=getattr(tool, "is_enabled", None),
            type=getattr(tool, "type", None),
            has_mcp_server=getattr(tool, "mcp_server", None) is not None,
        )

        if not tool.is_enabled:
            log.warningx(
                "Tool uitvoering afgebroken: tool is uitgeschakeld",
                tool_id=tool_id,
                tool_name=getattr(tool, "name", None),
            )
            raise HTTPException(status_code=400, detail="Tool is disabled")

        server = tool.mcp_server
        if not server:
            log.warningx(
                "Tool uitvoering afgebroken: tool heeft geen MCP server",
                tool_id=tool_id,
                tool_name=getattr(tool, "name", None),
            )
            raise HTTPException(status_code=400, detail="Tool has no MCP server")

        log.debugx(
            "MCP server gevonden voor tool uitvoering",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            mcp_server_id=getattr(server, "id", None),
            server_name=getattr(server, "name", None),
            base_url=getattr(server, "base_url", None),
            is_enabled=getattr(server, "is_enabled", None),
        )

        if not server.is_enabled:
            log.warningx(
                "Tool uitvoering afgebroken: MCP server is uitgeschakeld",
                tool_id=tool_id,
                tool_name=getattr(tool, "name", None),
                mcp_server_id=getattr(server, "id", None),
                server_name=getattr(server, "name", None),
            )
            raise HTTPException(status_code=400, detail="MCP server is disabled")

        auth = self.auth_repo.get_active_for_server(server.id)
        log.debugx(
            "Actieve MCP auth opgehaald voor tool uitvoering",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            mcp_server_id=getattr(server, "id", None),
            has_auth=auth is not None,
            auth_id=getattr(auth, "id", None) if auth else None,
            auth_type=getattr(auth, "auth_type", None) if auth else None,
        )

        client = self.client_factory.build(server=server, auth=auth)
        log.debugx(
            "MCP client gebouwd voor tool uitvoering",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            mcp_server_id=getattr(server, "id", None),
            server_name=getattr(server, "name", None),
        )

        remote_name = getattr(tool, "remote_name", None) or tool.name
        log.infox(
            "Remote MCP tool call uitvoeren",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            remote_name=remote_name,
            mcp_server_id=getattr(server, "id", None),
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
        )

        result = await client.call(remote_name, args)

        log.infox(
            "Tool uitvoering afgerond",
            tool_id=tool_id,
            tool_name=getattr(tool, "name", None),
            remote_name=remote_name,
            mcp_server_id=getattr(server, "id", None),
            result_type=type(result).__name__,
        )
        return result