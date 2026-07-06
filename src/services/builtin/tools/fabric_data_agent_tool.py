"""Builtin tool: query a Microsoft Fabric Data Agent in natural language.

Always-on (no skill needed). The orchestrator passes the configured agent's `name`
and a `question`; we resolve the agent, query it, and return the grounded answer
plus the query steps it ran. Connected agents are listed in the planner manifest
so the model knows which one to pick.
"""
from __future__ import annotations

from typing import Any, Dict

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


@internal_tool_registry.register(
    name="fabric_data_agent_query",
    title="Query a Fabric Data Agent",
    description=(
        "Ask a configured Microsoft Fabric Data Agent a natural-language question about its "
        "data (lakehouse/warehouse/semantic model). Returns the grounded answer plus the query "
        "steps the agent ran. Use this for data/analytics questions about connected Fabric datasets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Configured Fabric data agent name — see the connected Fabric data agents listed in the prompt (omit if only one is connected)."},
            "question": {"type": "string", "description": "The natural-language data question."},
        },
        "required": ["question"],
    },
    tags=["internal", "fabric", "data"],
)
async def fabric_data_agent_query(args: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = (args.get("agent") or "").strip()
    # Be tolerant of the field name: weaker models often put the request in `query`
    # (sometimes as raw SQL), `q`, `prompt`, or `text` instead of `question`. A Fabric
    # Data Agent takes a natural-language question but can interpret a query string too,
    # so accept any of these rather than erroring on a common naming slip.
    question = (
        args.get("question") or args.get("query") or args.get("q")
        or args.get("prompt") or args.get("text") or ""
    ).strip()
    if not question:
        return {"status": "error", "error": "Provide the data question in the `question` field (a natural-language question)."}

    from db.database import SessionLocal
    from services.fabric.fabric_data_agent_service import FabricDataAgentService, ask

    db = SessionLocal()
    try:
        svc = FabricDataAgentService(db)
        enabled = svc.list_enabled()
        if not enabled:
            return {"status": "error", "error": "No Fabric Data Agents are configured. Add one under AI Models → Fabric Data Agents."}
        if agent_name:
            agent = svc.get_by_name(agent_name)
        else:
            agent = enabled[0] if len(enabled) == 1 else None
        if agent is None or not agent.enabled:
            names = [a.name for a in enabled]
            return {"status": "error", "error": f"Unknown or disabled agent '{agent_name}'. Available: {names}"}
        result = await ask(agent, question)
        return {"status": "ok", "agent": agent.name, **result}
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the agent
        log.warningx("Fabric data agent query mislukt", agent=agent_name, error=str(exc))
        return {"status": "error", "error": f"Fabric query failed: {exc}"}
    finally:
        db.close()
