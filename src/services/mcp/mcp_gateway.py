"""
services/mcp/mcp_gateway.py

An MCP server that re-exposes ND3X's OWN tools (builtin tools like the board, and
every enabled MCP server such as Fabric) to the autonomous Claude Code CLI engine
in a workflow. ND3X stays the source of truth and the auth owner: the gateway
lists tools from the DB registry and delegates every call back to the MAIN server
(over a loopback HTTP endpoint, authenticated with an in-process shared secret) —
because stdio-backed tools like Fabric/OneLake, and their Azure session, are booted
subprocesses that live only in the main process. Execution therefore happens once,
where the runtime and auth actually are. (Without the delegation env the handler
falls back to executing locally — used by tests.)

Transport to the CLI is **stdio**: the CLI spawns this module as a subprocess and
talks over stdin/stdout (`--mcp-config` with a `command` entry). It runs on the
back-end host and reuses the same DB config (SessionLocal) for LISTING tools, so
it sees exactly the tools the agent sees.

Web tools are excluded on purpose — the CLI has its own WebSearch/WebFetch, so
routing those back through ND3X would be a wasted hop.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)

# Builtin tool names the CLI does better itself — don't re-expose (wasted hop).
_EXCLUDED_TOOL_NAMES = {"web_search", "web_fetch"}

# The MCP server name the CLI sees; tools show up as mcp__nd3x__<tool>.
SERVER_NAME = "nd3x"


async def _execute_tool(tool_id: int, args: Dict[str, Any]) -> Any:
    """Run one tool. When delegation env is set (the normal case), call back into
    the main server so stdio-backed tools (Fabric/OneLake) and their Azure session
    run there. Otherwise execute locally in this process (non-delegated / tests)."""
    url = os.environ.get("ND3X_INTERNAL_URL")
    token = os.environ.get("ND3X_INTERNAL_TOKEN")
    if url and token:
        return await _delegate_execute(url, token, tool_id, args)
    from db.database import SessionLocal
    from services.mcp.tool_execution_service import ToolExecutionService as _TES
    with SessionLocal() as call_db:
        return await _TES(call_db).execute_tool(tool_id, args)


async def _delegate_execute(url: str, token: str, tool_id: int, args: Dict[str, Any]) -> Any:
    """POST the tool call to the main server's internal execute endpoint."""
    import httpx
    endpoint = url.rstrip("/") + "/api/internal/mcp/execute"
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            endpoint,
            headers={"X-ND3X-Internal-Token": token},
            json={"tool_id": tool_id, "args": args},
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result") if isinstance(data, dict) else data


def _tool_to_schema(argument: Any) -> Dict[str, Any]:
    """A DB tool's `argument` is its JSON input schema; normalize to an object
    schema FastMCP accepts."""
    if isinstance(argument, dict) and argument.get("type") == "object":
        return argument
    if isinstance(argument, dict) and "properties" in argument:
        return {"type": "object", **argument}
    return {"type": "object", "properties": {}}


def _skill_scoped_tool_ids(db) -> Dict[int, set]:
    """tool_id → the ENABLED skill names it is linked to (via skill_tool). Tools
    that appear here are "skill-scoped": exposed only when one of their skills is
    selected for the turn. Tools without links stay always-on."""
    from models.skill import Skill
    from models.skill_tool import SkillTool

    out: Dict[int, set] = {}
    rows = (db.query(SkillTool, Skill)
            .join(Skill, Skill.id == SkillTool.skill_id)
            .filter(SkillTool.is_enabled == True,  # noqa: E712
                    Skill.is_enabled == True)      # noqa: E712
            .all())
    for link, skill in rows:
        if skill.name:
            out.setdefault(link.tool_id, set()).add(skill.name)
    return out


def _selected_skills_from_env() -> Optional[set]:
    """The turn's selected skills, passed by the parent via ND3X_GATEWAY_SKILLS
    (comma-separated). Absent → None → no filtering (legacy: expose everything);
    present-but-empty → filter to always-on tools only."""
    raw = os.environ.get("ND3X_GATEWAY_SKILLS")
    if raw is None:
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


def _list_gateway_tools(db, selected_skills: Optional[set] = None) -> List[Any]:
    """Enabled DB tools whose server is enabled, minus the excluded web tools.
    Each becomes one MCP tool that calls back into ToolExecutionService.

    ``selected_skills`` scopes skill-linked tools to the turn: a tool linked to
    one or more skills is only exposed when one of those skills was selected
    (None = no filtering — legacy behaviour for callers that don't pass skills).
    Unlinked tools are always exposed."""
    from fastmcp.tools.tool import FunctionTool
    from services.mcp.tool_execution_service import ToolExecutionService  # noqa: F401
    from repository.tool_repository import ToolRepository

    skill_scoped: Dict[int, set] = {}
    if selected_skills is not None:
        try:
            skill_scoped = _skill_scoped_tool_ids(db)
        except Exception as exc:  # noqa: BLE001 — filtering must never break the gateway
            log.warningx("gateway: skill-scoping overslaan", error=str(exc))
            skill_scoped = {}

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
        if selected_skills is not None:
            linked = skill_scoped.get(t.id)
            if linked and not (linked & selected_skills):
                continue  # skill-scoped tool whose skills weren't selected
        tool_id = t.id
        server_name = getattr(server, "name", None)

        def _make_handler(_tool_id: int):
            async def _handler(**kwargs: Any) -> Any:
                return await _execute_tool(_tool_id, kwargs or {})
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
    selected = _selected_skills_from_env()
    with SessionLocal() as db:
        tools = _list_gateway_tools(db, selected)
        for tool in tools:
            mcp.add_tool(tool)
    log.infox("MCP gateway (stdio) gebouwd", tool_count=len(tools),
              skill_scoped=selected is not None)
    return mcp


def mcp_config_for_cli(*, python: str | None = None, cwd: str | None = None,
                       selected_skills: List[str] | None = None) -> Dict[str, Any]:
    """The --mcp-config object the workflow engine writes for the CLI. Starts
    this module as a stdio server under the same interpreter + source root.

    ``selected_skills`` (the turn's selected skill names) scopes skill-linked
    tools: the child gateway then only exposes a linked tool when one of its
    skills is in this list. None = no scoping (expose everything, legacy)."""
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
    if selected_skills is not None:
        env["ND3X_GATEWAY_SKILLS"] = ",".join(s for s in selected_skills if s)
    # Delegation target: the child LISTS tools from the DB itself, but EXECUTES
    # them by calling back into this (main) process over HTTP, so stdio-backed
    # tools (Fabric/OneLake) and their Azure session run once, here. Without these
    # two vars the child falls back to executing locally (tests / non-delegated
    # use). HOST 0.0.0.0 means "all interfaces" — the child reaches us on loopback.
    try:
        from component.config import settings as _settings
        from services.mcp.internal_auth import INTERNAL_MCP_TOKEN
        _host = (getattr(_settings, "HOST", "") or "127.0.0.1")
        if _host in ("0.0.0.0", "", "::"):
            _host = "127.0.0.1"
        env["ND3X_INTERNAL_URL"] = f"http://{_host}:{int(getattr(_settings, 'PORT', 8088))}"
        env["ND3X_INTERNAL_TOKEN"] = INTERNAL_MCP_TOKEN
    except Exception:  # noqa: BLE001 — without these the child executes locally
        pass
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
