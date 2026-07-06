"""Web-research tool for the curiosity/cognition system.

Provider-aware so cognition works out of the box WITHOUT any API key: by default
it uses keyless DuckDuckGo, and only uses Exa when an Exa API key is configured.
Controlled by settings.RESEARCH_PROVIDER:

    "auto"        — Exa if EXA_API_KEY is set, else keyless DuckDuckGo (default)
    "duckduckgo"  — keyless DuckDuckGo
    "exa"         — Exa (requires EXA_API_KEY)
    "none"        — disabled

Registered under the stable internal name "exa_research" (the curiosity assistants
still call that name), so swapping the backend needs no prompt changes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from component.config import settings
from component.logging import get_logger
from services.system_tools.exa_research_tool import ExaResearchTool

log = get_logger(__name__)


class ResearchTool:
    name = "exa_research"

    def __init__(self) -> None:
        self._exa = ExaResearchTool(timeout_s=float(getattr(settings, "EXA_TIMEOUT_S", 30.0) or 30.0))

    def _provider(self) -> str:
        choice = (getattr(settings, "RESEARCH_PROVIDER", "auto") or "auto").strip().lower()
        if choice == "auto":
            return "exa" if (settings.EXA_API_KEY or "").strip() else "duckduckgo"
        return choice

    async def run(
        self,
        *,
        query: str,
        num_results: int = 5,
        include_text: bool = True,
        text_char_limit: int = 4000,
    ) -> Dict[str, Any]:
        provider = self._provider()
        log.infox("Research tool run", provider=provider, query_length=len((query or "")))

        if provider == "none":
            return {"ok": False, "error": "Research is disabled (RESEARCH_PROVIDER=none).",
                    "query": query, "results": []}
        if provider == "exa":
            return await self._exa.run(
                query=query, num_results=num_results,
                include_text=include_text, text_char_limit=text_char_limit,
            )
        # keyless default
        return await self._duckduckgo(query=query, num_results=num_results, text_char_limit=text_char_limit)

    async def _duckduckgo(self, *, query: str, num_results: int, text_char_limit: int) -> Dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "Missing query.", "query": query, "results": []}
        n = max(1, min(int(num_results or 5), 10))

        def _search() -> List[Dict[str, Any]]:
            from ddgs import DDGS
            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=n))

        try:
            raw = await asyncio.to_thread(_search)
        except Exception as exc:  # noqa: BLE001 — research failure must not crash the worker
            log.warningx("DuckDuckGo research failed", query=q, error=str(exc))
            return {"ok": False, "error": f"DuckDuckGo search failed: {exc}", "query": q, "results": []}

        results: List[Dict[str, Any]] = []
        for item in raw:
            body = (item.get("body") or "")
            if text_char_limit and len(body) > text_char_limit:
                body = body[:text_char_limit]
            results.append({
                "title": item.get("title"),
                "url": item.get("href") or item.get("url"),
                "published_date": None,
                "author": None,
                "score": None,
                "summary": item.get("body"),
                "text": body,
            })
        log.infox("DuckDuckGo research done", query=q, result_count=len(results))
        return {"ok": True, "query": q, "results": results, "raw_count": len(results)}
