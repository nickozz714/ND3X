"""
services/mcp/mcp_gateway.py

An MCP server that re-exposes ND3X's OWN tools (builtin tools like the board, and
every enabled MCP server such as Fabric) to the autonomous Claude Code CLI engine
in a workflow. ND3X stays the source of truth and the auth owner: the gateway
lists tools from the DB registry and delegates every call to ToolExecutionService,
which builds the right client and applies the server's auth exactly as the
orchestrator does. Fabric's tools, and their hops, therefore work through here.

Transport is **stdio**: the CLI spawns this module as a subprocess and talks over
stdin/stdout (`--mcp-config` with a `command` entry). No network, no mounting on
the API, no lifespan juggling — and no shared secret, because it's a child
process, not an open endpoint. It runs on the back-end host and reuses the same
DB config (SessionLocal), so it sees exactly the tools the agent sees.

Web tools are excluded on purpose — the CLI has its own WebSearch/WebFetch, so
routing those back through ND3X would be a wasted hop.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from component.logging import get_logger

log = get_logger(__name__)

# Builtin tool names the CLI does better itself — don't re-expose (wasted hop).
_EXCLUDED_TOOL_NAMES = {"web_search", "web_fetch"}

# The MCP server name the CLI sees; tools show up as mcp__nd3x__<tool>.
SERVER_NAME = "nd3x"


def _tool_to_schema(argument: Any) -> Dict[str, Any]:
    """A DB tool's `argument` is its JSON input schema; normalize to an object
    schema FastMCP accepts."""
    if isinstance(argument, dict) and argument.get("type") == "object":
        return argument
    if isinstance(argument, dict) and "properties" in argument:
        return {"type": "object", **argument}
    return {"type": "object", "properties": {}}


def _list_gateway_tools(db) -> List[Any]:
    """Enabled DB tools whose server is enabled, minus the excluded web tools.
    Each becomes one MCP tool that calls back into ToolExecutionService."""
    from fastmcp.tools.tool import FunctionTool
    from services.mcp.tool_execution_service import ToolExecutionService  # noqa: F401
    from repository.tool_repository import ToolRepository

    tools = ToolRepository(db).get_all_with_relations(skip=0, limit=2000)
    out: List[Any] = []
    for t in tools:
        if not getattr(t, "is_enabled", True):
            continue
        server = getattr(t, "mcp_server", None)
        if server is not None and not getattr(server, "is_enabled", True):
            continue
        name = (t.name or "").strip()
        if not name or name in _EXCLUDED_TOOL_NAMES:
            continue
        tool_id = t.id
        server_name = getattr(server, "name", None)

        def _make_handler(_tool_id: int):
            # Each call opens its own session + service so clients/auth resolve
            # fresh (the gateway process is long-lived across the CLI session).
            async def _handler(**kwargs: Any) -> Any:
                from db.database import SessionLocal
                from services.mcp.tool_execution_service import ToolExecutionService as _TES
                with SessionLocal() as call_db:
                    return await _TES(call_db).execute_tool(_tool_id, kwargs or {})
            return _handler

        out.append(FunctionTool(
            name=name,
            description=(t.description or name)[:1024],
            parameters=_tool_to_schema(t.argument),
            fn=_make_handler(tool_id),
            meta={"nd3x_tool_id": tool_id, "nd3x_server": server_name},
        ))
    return out


def _route_logging_to_stderr() -> None:
    """stdio MCP uses stdout for the JSON-RPC stream — any log line there
    corrupts the protocol. Send all ND3X logging to stderr instead.

    ND3X loggers take their stream from LoggerConfig's default at construction
    and are cached. So: flip the default (covers every logger built from here
    on, including ones created lazily during tool execution) and reconfigure the
    already-cached loggers (which rebuild from that now-stderr default)."""
    import logging as _logging

    try:
        import component.logging as _clog
        _clog.LoggerConfig.stream = sys.stderr
        try:
            _clog.reconfigure_logging()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    # Belt and suspenders: re-point any remaining stdout stream handlers.
    for lg in [_logging.getLogger()] + [
        _logging.getLogger(n) for n in list(_logging.root.manager.loggerDict)
    ]:
        for h in list(getattr(lg, "handlers", []) or []):
            if isinstance(h, _logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout:
                try:
                    h.setStream(sys.stderr)
                except Exception:  # noqa: BLE001
                    pass


def _load_models() -> None:
    """Import every model module so SQLAlchemy can resolve all relationship
    strings (the gateway runs as its own process; a partial registry raises
    KeyError on the first tool query)."""
    for m in (
        "authenticate", "audit", "assistant", "tool", "assistant_tool", "mcp_server",
        "assistant_output_chunk", "system_cognition", "log_entry", "application_settings",
        "skill", "skill_file", "assistant_skill", "skill_tool", "assistant_thread",
        "shell_script", "token_usage", "text_document", "provider", "fabric_data_agent",
        "transfer", "meeting_profile", "slash_command", "secret", "board", "workflow",
    ):
        try:
            __import__(f"models.{m}")
        except Exception:  # noqa: BLE001 — a missing optional model must not break the gateway
            pass


def build_server():
    """Build the FastMCP stdio server with the current ND3X tool set."""
    from fastmcp import FastMCP
    from db.database import SessionLocal

    _route_logging_to_stderr()
    _load_models()
    mcp = FastMCP(name="ND3X Gateway")
    with SessionLocal() as db:
        tools = _list_gateway_tools(db)
        for tool in tools:
            mcp.add_tool(tool)
    log.infox("MCP gateway (stdio) gebouwd", tool_count=len(tools))
    return mcp


def mcp_config_for_cli(*, python: str | None = None, cwd: str | None = None) -> Dict[str, Any]:
    """The --mcp-config object the workflow engine writes for the CLI. Starts
    this module as a stdio server under the same interpreter + source root."""
    py = python or sys.executable
    src_root = cwd or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Quiet the child at the source: stdout is the JSON-RPC stream, so keep ND3X
    # logging off it. ERROR level + no DB/file log handler + stderr routing (in
    # main) means no INFO/DEBUG lines can corrupt the protocol.
    env = {
        "PYTHONPATH": src_root,
        "LOG_LEVEL": "ERROR",
        "LOG_DB_ENABLED": "false",
        "LOG_FILE": "",
    }
    # Pass through the DB/config env so the child sees the same registry.
    for k in ("SQLITE_PATH", "SQLITE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME",
              "DB_USER", "DB_PASSWORD", "DB_PORT", "MYSQL_URL", "ND3X_DATA_DIR",
              "NDX_CONFIG", "ANTHROPIC_BASE_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": py,
                "args": ["-m", "services.mcp.mcp_gateway"],
                "cwd": src_root,
                "env": env,
            }
        }
    }


def main() -> None:
    """stdio entrypoint — the CLI runs `python -m services.mcp.mcp_gateway`."""
    # Ensure src root on path when run as a module from an arbitrary cwd.
    src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src_root not in sys.path:
        sys.path.insert(0, src_root)
    _route_logging_to_stderr()
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
