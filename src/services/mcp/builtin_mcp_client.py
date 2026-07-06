"""
services/mcp/builtin_mcp_client.py  (uitgebreid met internal tools)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi.encoders import jsonable_encoder

from component.logging import get_logger
from services.shell.az_login_service import AzLoginService
from services.shell.shell_exec_service import exec_command, exec_script
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)

_STATIC_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "system__az_login",
        "title": "Azure Login",
        "description": (
            "Start een Azure device code login flow. Retourneert een URL en code. "
            "Poll daarna system__az_login_status om te controleren of de login geslaagd is."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "outputSchema": None, "annotations": {}, "meta": {},
        "tags": ["system", "auth"], "fastmcp": {},
    },
    {
        "name": "system__az_login_status",
        "title": "Azure Login Status",
        "description": "Controleer de status van een lopende az login flow. Retourneert status: idle | pending | authenticated | failed.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "outputSchema": None, "annotations": {}, "meta": {},
        "tags": ["system", "auth"], "fastmcp": {},
    },
    {
        "name": "system__shell_exec",
        "title": "Shell Exec",
        "description": "Voer een los bash commando uit in de container en retourneer stdout, stderr en exit_code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Het bash commando om uit te voeren"},
                "timeout": {"type": "number", "description": "Timeout in seconden (standaard 60)"},
            },
            "required": ["command"],
        },
        "outputSchema": None, "annotations": {}, "meta": {},
        "tags": ["system", "shell"], "fastmcp": {},
    },
]


def _script_to_tool(script) -> Dict[str, Any]:
    parameters = script.parameters or []
    return {
        "name": f"shell__{script.slug}",
        "title": script.name,
        "description": script.description or f"Voer script '{script.slug}' uit",
        "inputSchema": {
            "type": "object",
            "properties": {p: {"type": "string", "description": p} for p in parameters},
            "required": parameters,
        },
        "outputSchema": None, "annotations": {}, "meta": {"slug": script.slug},
        "tags": ["shell"], "fastmcp": {},
        "raw": {"slug": script.slug, "parameters": parameters},
    }


def _ok(data: Any) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(jsonable_encoder(data), ensure_ascii=False)}],
        "isError": False,
    }


def _err(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class BuiltinMCPClient:
    def __init__(self, az_login_service: AzLoginService, db_factory):
        self._az = az_login_service
        self._db_factory = db_factory
        log.debugx("BuiltinMCPClient aangemaakt")

    def _load_script_tools(self) -> List[Dict[str, Any]]:
        from repository.shell_script_repository import ShellScriptRepository
        db = self._db_factory()
        try:
            return [_script_to_tool(s) for s in ShellScriptRepository(db).get_all(only_enabled=True)]
        finally:
            db.close()

    def _all_tools(self) -> List[Dict[str, Any]]:
        return (
            _STATIC_TOOLS
            + self._load_script_tools()
            + internal_tool_registry.list_tools()
        )

    async def list_tools(self) -> List[Dict[str, Any]]:
        return self._all_tools()

    async def list_tools_listing(
        self,
        *,
        name_contains: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        items = self._all_tools()
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

    async def call(self, tool: str, args: Dict[str, Any]) -> Any:
        log.infox("BuiltinMCPClient call", tool=tool, arg_keys=list(args.keys()) if isinstance(args, dict) else None)

        if tool == "system__az_login":
            try:
                return _ok(await self._az.start_device_code())
            except Exception as err:
                return _err(str(err))

        if tool == "system__az_login_status":
            return _ok(self._az.get_status())

        if tool == "system__shell_exec":
            command = (args or {}).get("command")
            if not command:
                return _err("`command` is verplicht")
            try:
                return _ok(await exec_command(command, timeout=float((args or {}).get("timeout") or 60)))
            except Exception as err:
                return _err(str(err))

        if tool.startswith("shell__"):
            return await self._run_script(tool[len("shell__"):], args or {})

        if internal_tool_registry.has_tool(tool):
            try:
                result = await internal_tool_registry.call(tool, args or {})
                return _ok(result)
            except Exception as err:
                log.errorx("Internal tool fout", tool=tool, error=str(err))
                return _err(str(err))

        return _err(f"Onbekende builtin tool: '{tool}'")

    async def _run_script(self, slug: str, args: Dict[str, str]) -> Dict[str, Any]:
        from repository.shell_script_repository import ShellScriptRepository
        db = self._db_factory()
        try:
            script = ShellScriptRepository(db).get_by_slug(slug)
            if not script:
                return _err(f"Script '{slug}' niet gevonden")
            if not script.is_enabled:
                return _err(f"Script '{slug}' is uitgeschakeld")
            try:
                return _ok(await exec_script(
                    script=script.script,
                    parameters=script.parameters or [],
                    env_defaults=script.env or {},
                    args=args,
                ))
            except ValueError as err:
                return _err(str(err))
            except Exception as err:
                log.errorx("Script uitvoering mislukt", slug=slug, error=str(err))
                return _err(f"Script uitvoering mislukt: {err}")
        finally:
            db.close()