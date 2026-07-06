"""
services/builtin/tools/secret_tools.py

Builtin tool that lets the agent DISCOVER available secrets by name only. It
never returns values: the agent references a secret as ``${secret.NAME}`` and
the value is injected server-side at the outbound boundary (e.g. a workflow
http_request), so the model never sees the plaintext.

Registered on import (imported in ask_job_callbacks.py).
"""
from __future__ import annotations

from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


@internal_tool_registry.register(
    name="secret__list",
    title="List Secrets",
    description=(
        "List available secret names (and descriptions/tags) from the KeyVault. "
        "Values are NEVER returned. To use a secret, reference it as "
        "${secret.NAME} in a workflow http_request — the value is injected "
        "server-side and stays hidden from you."
    ),
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "secret"],
)
async def secret_list(_args: Dict[str, Any]) -> Dict[str, Any]:
    from db.database import SessionLocal
    from services.secret_service import SecretService
    with SessionLocal() as db:
        rows = SecretService(db).list()
        return {
            "status": "success",
            "secrets": [
                {
                    "name": r.name,
                    "description": (r.description or "")[:200],
                    "tags": list(r.tags or []),
                    "has_value": r.value_encrypted is not None,
                }
                for r in rows
            ],
            "note": "Reference a secret as ${secret.NAME} in a workflow http_request; values are never exposed.",
        }
