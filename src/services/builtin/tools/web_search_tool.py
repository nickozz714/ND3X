"""Builtin web_search tool — provider-native web search, gated by the
`chat.web_search` routing slot. Lets the orchestrator look things up (e.g. to
research a connector API, or for live meeting look-ups). Returns "not enabled"
guidance when the slot is unassigned or the provider has no native search.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from services.builtin.internal_tool_registry import internal_tool_registry


@internal_tool_registry.register(
    name="web_search",
    title="Web search",
    description=(
        "Search the web for current information using the agent model's native web search "
        "(OpenAI/Anthropic/Gemini). Returns a grounded answer. If the active model doesn't "
        "support web search it returns guidance instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "max_results": {"type": "integer", "description": "Hint for max sources (default 5)."},
        },
        "required": ["query"],
    },
    tags=["internal", "web"],
)
async def web_search(args: Dict[str, Any]) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"status": "error", "error": "query is required"}
    max_results = int(args.get("max_results") or 5)

    from db.database import SessionLocal
    from services.web_search_service import search

    db = SessionLocal()
    try:
        return await asyncio.to_thread(search, db, query, max_results=max_results)
    finally:
        db.close()
