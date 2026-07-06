from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi.encoders import jsonable_encoder

from component.logging import get_logger
from services.mcp.stdio_process_manager import StdioProcessManager

log = get_logger(__name__)


def _normalize_stdio_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        content = result.get("content", [])
        if not content:
            return jsonable_encoder(result)
        if all(
            isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            for b in content
        ):
            parsed = []
            for block in content:
                text = block["text"].strip()
                if (text.startswith("{") and text.endswith("}")) or \
                   (text.startswith("[") and text.endswith("]")):
                    try:
                        parsed.append(json.loads(text))
                        continue
                    except Exception:
                        pass
                parsed.append(text)
            if len(parsed) == 1:
                return jsonable_encoder(parsed[0])
            if all(isinstance(p, list) for p in parsed):
                flat: list = []
                for p in parsed:
                    flat.extend(p)
                return jsonable_encoder(flat)
            return jsonable_encoder(parsed)
        return jsonable_encoder(content)
    return jsonable_encoder(result)


class StdioMCPClient:
    """
    Drop-in vervanging voor MCPClient voor stdio servers.
    Zelfde publieke interface: call(), list_tools_listing(), list_tools(), get_tool()
    """

    def __init__(self, server_slug: str, process_manager: StdioProcessManager):
        self.server_slug = server_slug
        self._pm = process_manager
        log.debugx("StdioMCPClient aangemaakt", server_slug=server_slug)

    def _handle(self):
        handle = self._pm.get_handle(self.server_slug)
        if not handle or not handle.is_running():
            raise RuntimeError(
                f"Stdio server '{self.server_slug}' is niet actief. "
                "Controleer of de server is opgestart en het commando klopt."
            )
        return handle

    async def call(self, tool: str, args: Dict[str, Any]) -> Any:
        log.infox("StdioMCPClient call gestart", server_slug=self.server_slug, tool=tool, arg_keys=list(args.keys()) if isinstance(args, dict) else None)
        handle = self._handle()
        raw = await handle.call_tool(tool, args)
        result = _normalize_stdio_result(raw)
        log.infox("StdioMCPClient call afgerond", server_slug=self.server_slug, tool=tool)
        return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._handle().tools)

    async def list_tools_listing(
        self,
        *,
        name_contains: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        items = list(self._handle().tools)
        if name_contains:
            needle = name_contains.lower().strip()
            items = [t for t in items if needle in (t.get("name") or "").lower()]
        if tag:
            items = [t for t in items if tag in (t.get("tags") or [])]
        return {
            "count": len(items),
            "tools": items,
            "tool_names": [t["name"] for t in items if t.get("name")],
            "by_name": {t["name"]: t for t in items if t.get("name")},
        }

    async def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        listing = await self.list_tools_listing()
        return listing.get("by_name", {}).get(name)
