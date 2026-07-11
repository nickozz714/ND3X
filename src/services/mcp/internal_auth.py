"""Process-wide shared secret for internal server↔gateway calls.

The Claude Code MCP gateway runs as a SEPARATE stdio subprocess (spawned by the
CLI, which the chat agent / workflow runner starts). It can list ND3X tools from
the DB itself, but it must NOT execute stdio-backed tools (Fabric/OneLake, …)
locally: those live as booted subprocesses — with their Azure session — inside
the MAIN server process only. So the gateway delegates execution back to the main
server over HTTP.

This token authenticates that internal call. It is generated fresh per main-server
process (never persisted), held in memory here, injected into the gateway child's
env by ``mcp_config_for_cli`` and validated by the internal execute endpoint —
both of which run in / originate from this same process, so they read the same
value. A gateway that can't present it is rejected; a stray request without it is
rejected.
"""
from __future__ import annotations

import secrets

# Generated once at import (main-server process). The gateway child receives this
# via env (ND3X_INTERNAL_TOKEN) and echoes it back on each execute call.
INTERNAL_MCP_TOKEN: str = secrets.token_urlsafe(32)


def verify_internal_token(presented: str | None) -> bool:
    """Constant-time compare of a presented token against the process token."""
    if not presented:
        return False
    return secrets.compare_digest(presented, INTERNAL_MCP_TOKEN)
