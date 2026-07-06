"""
services/builtin/internal_tool_registry.py

Registry voor Python functies die als builtin MCP tools worden aangeboden.
Registreer tools met @internal_tool_registry.register(...)

Voorbeeld:
    from services.builtin.internal_tool_registry import internal_tool_registry

    @internal_tool_registry.register(
        name="text__ingest",
        title="Ingest Text",
        description="Ingest text content into the index.",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
            },
            "required": ["content"],
        },
    )
    async def my_tool(args: dict) -> dict:
        ...
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)


class InternalToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable] = {}

    def register(
        self,
        *,
        name: str,
        title: str,
        description: str,
        input_schema: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ):
        """Decorator om een async functie als internal tool te registreren."""
        def decorator(fn: Callable) -> Callable:
            self._tools[name] = {
                "name": name,
                "title": title,
                "description": description,
                "inputSchema": input_schema or {"type": "object", "properties": {}},
                "outputSchema": None,
                "annotations": {},
                "meta": {"source": "internal"},
                "tags": tags or ["internal"],
                "fastmcp": {},
            }
            self._handlers[name] = fn
            log.debugx("Internal tool geregistreerd", name=name)
            return fn
        return decorator

    def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    async def call(self, name: str, args: Dict[str, Any]) -> Any:
        handler = self._handlers.get(name)
        if not handler:
            raise ValueError(f"Internal tool '{name}' niet gevonden")
        log.infox("Internal tool aanroepen", name=name, arg_keys=list(args.keys()))
        result = await handler(args)
        log.infox("Internal tool afgerond", name=name)
        return result


# Singleton — wordt geïmporteerd door tools en door BuiltinMCPClient
internal_tool_registry = InternalToolRegistry()