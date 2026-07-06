from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from component.logging import get_logger

log = get_logger(__name__)

_INIT_TIMEOUT = 30.0
_CALL_TIMEOUT = 120.0


class StdioServerHandle:
    def __init__(self, name: str, command: str):
        self.name = name
        self.command = command
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._id_counter = 1
        self._buffer = ""
        self._ready = False
        self.tools: List[Dict[str, Any]] = []

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._process:
            raise RuntimeError(f"Stdio server '{self.name}' draait al")

        log.infox("Stdio MCP server starten", name=self.name, command=self.command)

        self._process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        asyncio.ensure_future(self._drain_stderr())
        await asyncio.wait_for(self._initialize(), timeout=_INIT_TIMEOUT)
        await asyncio.wait_for(self._discover_tools(), timeout=_INIT_TIMEOUT)
        self._ready = True
        log.infox("Stdio MCP server klaar", name=self.name, tool_count=len(self.tools))

    def stop(self) -> None:
        if self._process:
            log.infox("Stdio MCP server stoppen", name=self.name)
            try:
                self._process.kill()
            except Exception:
                pass
            self._process = None
            self._ready = False
            self.tools = []

    def is_running(self) -> bool:
        return self._process is not None and self._ready

    # ── JSON-RPC ───────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        _id = self._id_counter
        self._id_counter += 1
        return _id

    async def _send(self, obj: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError(f"Server '{self.name}' stdin niet beschikbaar")
        self._process.stdin.write((json.dumps(obj) + "\n").encode())
        await self._process.stdin.drain()

    async def _read_line(self) -> str:
        while True:
            if "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                return line.strip()
            if not self._process or not self._process.stdout:
                raise RuntimeError(f"Server '{self.name}' stdout niet beschikbaar")
            chunk = await self._process.stdout.read(4096)
            if not chunk:
                raise RuntimeError(f"Server '{self.name}' stdout gesloten")
            self._buffer += chunk.decode(errors="replace")

    async def _request(self, method: str, params: Dict[str, Any] = {}) -> Any:
        _id = self._next_id()
        await self._send({"jsonrpc": "2.0", "id": _id, "method": method, "params": params})
        while True:
            raw = await self._read_line()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.debugx("Stdio server niet-JSON stdout", name=self.name, raw=raw[:120])
                continue
            if msg.get("id") != _id:
                continue
            if "error" in msg:
                raise RuntimeError(f"MCP fout van '{self.name}': {msg['error'].get('message', msg['error'])}")
            return msg.get("result")

    async def _drain_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            async for line in self._process.stderr:
                log.debugx("Stdio server stderr", name=self.name, line=line.decode(errors="replace").rstrip())
        except Exception:
            pass

    # ── MCP Protocol ──────────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "orchestrator-stdio", "version": "1.0.0"},
        })
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        log.debugx("Stdio MCP initialize afgerond", name=self.name)

    async def _discover_tools(self) -> None:
        try:
            result = await self._request("tools/list", {})
            raw_tools = result.get("tools", []) if result else []
            self.tools = [
                {
                    "name": t.get("name"),
                    "title": t.get("title"),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema") or {},
                    "outputSchema": t.get("outputSchema"),
                    "annotations": t.get("annotations") or {},
                    "meta": t.get("meta") or {},
                    "tags": [],
                    "fastmcp": {},
                    "raw": t,
                }
                for t in raw_tools
            ]
            log.debugx("Stdio tools ontdekt", name=self.name, count=len(self.tools))
        except Exception as err:
            log.warningx("Stdio tools/list mislukt", name=self.name, error=str(err))
            self.tools = []

    async def call_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        if not self._ready:
            raise RuntimeError(f"Stdio server '{self.name}' is niet klaar")
        async with self._lock:
            log.infox("Stdio tool call uitvoeren", name=self.name, tool=tool_name, arg_keys=list(args.keys()))
            result = await asyncio.wait_for(
                self._request("tools/call", {"name": tool_name, "arguments": args}),
                timeout=_CALL_TIMEOUT,
            )
            log.infox("Stdio tool call afgerond", name=self.name, tool=tool_name)
            return result

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        await self._discover_tools()
        return self.tools


class StdioProcessManager:
    def __init__(self) -> None:
        self._servers: Dict[str, StdioServerHandle] = {}
        log.debugx("StdioProcessManager aangemaakt")

    async def start_server(self, name: str, command: str) -> StdioServerHandle:
        if name in self._servers and self._servers[name].is_running():
            log.debugx("Stdio server draait al", name=name)
            return self._servers[name]
        handle = StdioServerHandle(name=name, command=command)
        await handle.start()
        self._servers[name] = handle
        return handle

    async def stop_server(self, name: str) -> None:
        handle = self._servers.pop(name, None)
        if handle:
            handle.stop()

    async def restart_server(self, name: str, command: str) -> StdioServerHandle:
        await self.stop_server(name)
        return await self.start_server(name, command)

    def get_handle(self, name: str) -> Optional[StdioServerHandle]:
        return self._servers.get(name)

    def is_running(self, name: str) -> bool:
        handle = self._servers.get(name)
        return handle is not None and handle.is_running()

    def stop_all(self) -> None:
        for handle in self._servers.values():
            handle.stop()
        self._servers.clear()

    async def boot_from_db(self, db) -> None:
        from repository.mcp_server_repository import MCPServerRepository
        repo = MCPServerRepository(db)
        for server in repo.get_enabled():
            if getattr(server, "server_type", "http") != "stdio":
                continue
            command = getattr(server, "stdio_command", None)
            if not command:
                log.warningx("Stdio server heeft geen stdio_command, overgeslagen", name=server.name)
                continue
            try:
                await self.start_server(name=server.slug, command=command)
                log.infox("Stdio server opgestart vanuit database", name=server.name, slug=server.slug)
            except Exception as err:
                log.errorx("Stdio server opstarten mislukt", name=server.name, error=str(err))
