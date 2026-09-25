"""The live set of MCP servers: what is configured, what is connected, what
tools that gives the model.

Servers are stored in settings, not in code, so they survive restarts and can be
edited in the UI. Adding a server does not connect it - enabling it does, and
enabling is always an explicit act.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..tools.base import Tool, classify, namespace
from .protocol import MCPClient, MCPError, build_client


class MCPRegistry:
    def __init__(self) -> None:
        self.clients: dict[str, MCPClient] = {}
        self._lock = asyncio.Lock()

    # -- stored configuration ---------------------------------------------

    @staticmethod
    def configured() -> dict[str, dict[str, Any]]:
        return dict(settings.get("mcp_servers") or {})

    @staticmethod
    def _save(servers: dict[str, dict[str, Any]]) -> None:
        settings.set("mcp_servers", servers)

    def add(self, name: str, spec: dict[str, Any], origin: str = "manual",
            enabled: bool = False) -> None:
        servers = self.configured()
        servers[name] = {"spec": spec, "origin": origin, "enabled": bool(enabled)}
        self._save(servers)

    def remove(self, name: str) -> None:
        servers = self.configured()
        servers.pop(name, None)
        self._save(servers)

    # -- connection -------------------------------------------------------

    async def enable(self, name: str) -> dict[str, Any]:
        servers = self.configured()
        entry = servers.get(name)
        if not entry:
            return {"ok": False, "error": f"No server called {name!r}"}

        async with self._lock:
            await self._disconnect(name)
            try:
                client = build_client(name, entry["spec"])
            except MCPError as exc:
                return {"ok": False, "error": str(exc)}

            connected = await client.connect()
            self.clients[name] = client

        entry["enabled"] = connected
        servers[name] = entry
        self._save(servers)

        if not connected:
            return {"ok": False, "error": client.error or "Could not connect"}
        return {"ok": True, "tools": len(client.tools),
                "server_info": client.server_info}

    async def disable(self, name: str) -> dict[str, Any]:
        async with self._lock:
            await self._disconnect(name)
        servers = self.configured()
        if name in servers:
            servers[name]["enabled"] = False
            self._save(servers)
        return {"ok": True}

    async def _disconnect(self, name: str) -> None:
        client = self.clients.pop(name, None)
        if client:
            await client.close()

    async def connect_enabled(self) -> dict[str, Any]:
        """Called at startup: bring up every server marked enabled."""
        results: dict[str, Any] = {}
        for name, entry in self.configured().items():
            if entry.get("enabled"):
                results[name] = await self.enable(name)
        return results

    async def shutdown(self) -> None:
        async with self._lock:
            for name in list(self.clients):
                await self._disconnect(name)

    async def reconnect(self, name: str) -> dict[str, Any]:
        return await self.enable(name)

    # -- tools ------------------------------------------------------------

    def catalogue(self) -> list[Tool]:
        """Every tool from every connected server, namespaced and classified."""
        out: list[Tool] = []
        for server, client in self.clients.items():
            if not client.connected:
                continue
            for mcp_tool in client.tools:
                out.append(Tool(
                    name=namespace(server, mcp_tool.name),
                    raw_name=mcp_tool.name,
                    description=mcp_tool.description,
                    input_schema=mcp_tool.input_schema,
                    server=server,
                    origin="mcp",
                    risk=classify(mcp_tool.name, mcp_tool.annotations,
                                  mcp_tool.description),
                    annotations=mcp_tool.annotations,
                ))
        return out

    def find(self, namespaced: str) -> tuple[str, str] | None:
        for tool in self.catalogue():
            if tool.name == namespaced:
                return tool.server, tool.raw_name
        return None

    async def call(self, namespaced: str, arguments: dict[str, Any]):
        target = self.find(namespaced)
        if not target:
            from .protocol import ToolCallResult
            return ToolCallResult(ok=False,
                                  text=f"No such tool: {namespaced}")
        server, raw = target
        client = self.clients.get(server)
        if not client or not client.connected:
            from .protocol import ToolCallResult
            return ToolCallResult(ok=False,
                                  text=f"Server {server!r} is not connected.")
        return await client.call(raw, arguments)

    # -- reporting --------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        out = []
        for name, entry in sorted(self.configured().items()):
            client = self.clients.get(name)
            spec = entry.get("spec") or {}
            out.append({
                "name": name,
                "origin": entry.get("origin", "manual"),
                "enabled": bool(entry.get("enabled")),
                "connected": bool(client and client.connected),
                "error": client.error if client else "",
                "transport": "http" if spec.get("url") else "stdio",
                "summary": spec.get("url") or " ".join(
                    [str(spec.get("command", ""))]
                    + [str(a) for a in (spec.get("args") or [])[:4]]).strip(),
                "server_info": client.server_info if client else {},
                "tools": [
                    {"name": namespace(name, t.name), "raw_name": t.name,
                     "description": t.description[:200],
                     "risk": classify(t.name, t.annotations, t.description)}
                    for t in (client.tools if client else [])
                ],
            })
        return out


registry = MCPRegistry()
