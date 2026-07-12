"""
services/providers/cli_agent_runner.py

Shared machinery for running work through a CLI-AGENT provider — a provider whose
class advertises ``is_cli_agent`` (Claude Code now; Codex/others later). It runs
its OWN agent loop with its own tools ("agent" execution mode), and ND3X ships it
the ND3X skills/MCP/tools via the stdio gateway.

The subsystem runners (chat agent, workflow step, and — Fase 3 — cognition)
subclass this and add only what differs: the instruction, the prompt/context, and
how the result is parsed. The base owns the parts they all repeat:
- resolving the enabled CLI-agent provider **by capability** (is_cli_agent), NOT by
  the ``provider_type == "claude_code"`` string — so a second CLI agent needs no
  change here;
- writing/cleaning the ND3X MCP gateway ``--mcp-config`` temp file;
- tolerant extraction of the last JSON object from the agent's final message
  (the output-contract/"envelope" pattern), since CLI agents can't enforce a schema.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)


class CliAgentRunner:
    """Base for subsystem runners that delegate to a CLI-agent provider."""

    def __init__(self, db: Session):
        self.db = db

    # ── Provider resolution (capability-based) ──────────────────────────────────

    def _resolve_cli_provider_row(self):
        """First enabled provider whose ChatProvider class has ``is_cli_agent``.
        Capability-based so any CLI agent (Claude Code, future Codex) is found
        without a name check here."""
        from models.provider import Provider
        from services.providers.base import ChatProvider

        # Ensure the concrete provider classes are imported so they auto-register
        # in ChatProvider._type_registry (class_for_type needs them present).
        try:
            import services.providers.claude_code_provider  # noqa: F401
        except Exception:  # noqa: BLE001 — a missing optional provider must not break resolution
            pass

        rows = (self.db.query(Provider)
                .filter(Provider.enabled == True)  # noqa: E712
                .order_by(Provider.id.asc())
                .all())
        for p in rows:
            cls = ChatProvider.class_for_type(getattr(p, "provider_type", None))
            if cls is not None and getattr(cls, "is_cli_agent", False):
                return p
        return None

    def cli_agent_available(self) -> bool:
        return self._resolve_cli_provider_row() is not None

    # ── ND3X MCP gateway config lifecycle ───────────────────────────────────────

    @staticmethod
    def write_gateway_config(prefix: str = "nd3x-mcp-") -> Optional[str]:
        """Write the ND3X MCP gateway ``--mcp-config`` to a temp file so the CLI
        agent gets ND3X's tools/skills/MCP servers via the stdio gateway. Returns
        the path (caller passes it to the provider and unlinks after the run), or
        ``None`` when it couldn't be written — the run then proceeds without ND3X
        tools rather than failing."""
        try:
            from services.mcp.mcp_gateway import mcp_config_for_cli
            fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(mcp_config_for_cli(), f)
            return path
        except Exception as exc:  # noqa: BLE001 — the run can still proceed without ND3X tools
            log.warningx("ND3X MCP gateway config schrijven mislukt — run draait zonder ND3X-tools",
                         error=str(exc))
            return None

    @staticmethod
    def cleanup_gateway_config(path: Optional[str]) -> None:
        if path:
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass

    # ── Output contract / envelope parsing ──────────────────────────────────────

    @staticmethod
    def last_json_object(text: str) -> Any:
        """The last balanced ``{...}`` object in ``text``, parsed — tolerant of
        prose before/after it. A CLI agent is instructed to end with one JSON
        envelope but can't be forced to; this recovers it. Returns None if none."""
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
        depth = 0
        end = -1
        for i in range(len(text) - 1, -1, -1):
            c = text[i]
            if c == "}":
                if depth == 0:
                    end = i
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0 and end != -1:
                    try:
                        return json.loads(text[i:end + 1])
                    except Exception:  # noqa: BLE001
                        end = -1
        return None
