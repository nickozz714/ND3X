from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from component.config import settings
from component.logging import get_logger


log = get_logger(__name__)


class ExaResearchTool:
    name = "exa_research"

    def __init__(self, api_key: Optional[str] = None, timeout_s: float = 30.0):
        log.debugx(
            "ExaResearchTool initialiseren",
            api_key_provided=api_key is not None,
            has_env_api_key=bool(settings.EXA_API_KEY),
            timeout_s=timeout_s,
        )
        self.api_key = api_key or settings.EXA_API_KEY
        self.timeout_s = timeout_s
        log.debugx(
            "ExaResearchTool geïnitialiseerd",
            has_api_key=bool(self.api_key),
            timeout_s=self.timeout_s,
        )

    async def run(
        self,
        *,
        query: str,
        num_results: int = 5,
        include_text: bool = True,
        text_char_limit: int = 4000,
    ) -> Dict[str, Any]:
        log.infox(
            "EXA research tool run gestart",
            query_length=len(query or ""),
            num_results=num_results,
            include_text=include_text,
            text_char_limit=text_char_limit,
            has_api_key=bool(self.api_key),
            timeout_s=self.timeout_s,
        )

        if not self.api_key:
            log.warningx(
                "EXA research tool afgebroken: EXA_API_KEY ontbreekt",
                query_length=len(query or ""),
            )
            return {
                "ok": False,
                "error": "EXA_API_KEY is not configured.",
                "query": query,
                "results": [],
            }

        query = (query or "").strip()
        log.debugx(
            "EXA research query genormaliseerd",
            query=query,
            query_length=len(query),
        )
        if not query:
            log.warningx("EXA research tool afgebroken: query ontbreekt")
            return {
                "ok": False,
                "error": "Missing query.",
                "query": query,
                "results": [],
            }

        num_results = max(1, min(int(num_results or 5), 10))
        log.debugx(
            "EXA research num_results genormaliseerd",
            query=query,
            num_results=num_results,
        )

        payload = {
            "query": query,
            "numResults": num_results,
            "type": "auto",
            "contents": {
                "text": include_text,
                "summary": True,
            },
        }
        log.debugx(
            "EXA research request payload opgebouwd",
            query=query,
            numResults=payload["numResults"],
            type=payload["type"],
            include_text=payload["contents"]["text"],
            include_summary=payload["contents"]["summary"],
        )

        log.infox(
            "EXA API request uitvoeren",
            query=query,
            num_results=num_results,
            include_text=include_text,
            timeout_s=self.timeout_s,
        )
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                "https://api.exa.ai/search",
                headers={
                    "x-api-key": self.api_key,
                    "content-type": "application/json",
                },
                json=payload,
            )

        log.infox(
            "EXA API response ontvangen",
            query=query,
            status_code=resp.status_code,
            response_length=len(resp.text or ""),
        )

        if resp.status_code >= 400:
            log.warningx(
                "EXA API gaf foutstatus terug",
                query=query,
                status_code=resp.status_code,
                response_preview=resp.text[:1000],
            )
            return {
                "ok": False,
                "error": f"EXA returned HTTP {resp.status_code}: {resp.text[:1000]}",
                "query": query,
                "results": [],
            }

        data = resp.json()
        log.debugx(
            "EXA API response JSON geparsed",
            query=query,
            data_keys=list(data.keys()) if isinstance(data, dict) else None,
            raw_result_count=len(data.get("results") or []) if isinstance(data, dict) else None,
        )
        results: List[Dict[str, Any]] = []

        for item in data.get("results") or []:
            log.debugx(
                "EXA result item verwerken",
                query=query,
                title=item.get("title"),
                url=item.get("url"),
                has_text=bool(item.get("text")),
                text_length=len(item.get("text") or ""),
                has_summary=bool(item.get("summary")),
                score=item.get("score"),
            )
            text = item.get("text") or ""
            if text_char_limit and len(text) > text_char_limit:
                log.debugx(
                    "EXA result text inkorten",
                    query=query,
                    title=item.get("title"),
                    original_length=len(text),
                    text_char_limit=text_char_limit,
                )
                text = text[:text_char_limit]

            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "published_date": item.get("publishedDate"),
                    "author": item.get("author"),
                    "score": item.get("score"),
                    "summary": item.get("summary"),
                    "text": text,
                }
            )

        log.infox(
            "EXA research tool run afgerond",
            query=query,
            result_count=len(results),
            raw_count=len(results),
        )
        return {
            "ok": True,
            "query": query,
            "results": results,
            "raw_count": len(results),
        }