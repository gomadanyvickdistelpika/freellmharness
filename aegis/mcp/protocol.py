"""MCP client: JSON-RPC 2.0 over stdio or streamable HTTP.

One MCPClient owns one server connection. It performs the handshake, keeps a
live tool list, and calls tools. Failures are surfaced as errors rather than
swallowed - a server that will not start must say so loudly, because a silently
missing tool looks to the model like a capability that does not exist.

Transports
    stdio   newline-delimited JSON on a subprocess's stdin/stdout. What Codex,
            Claude Desktop and VS Code all use for local servers.
    http    streamable HTTP: JSON-RPC POSTed to one endpoint, replies as either
            JSON or an SSE stream. Used by hosted servers.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aegis", "version": "0.2.0"}

_WINDOWS = sys.platform == "win32"
DEFAULT_TIMEOUT = 45.0
STARTUP_TIMEOUT = 30.0


class MCPError(Exception):
    pass


def resolve_command(command: str) -> str | None:
    """Find an executable, including the .cmd shims npm writes on Windows."""
    if os.path.isabs(command) and os.path.exists(command):
        return command
    if found := shutil.which(command):
        return found
    if _WINDOWS:
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            if found := shutil.which(command + ext):
                return found
    return None


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str],
                 cwd: str | None = None) -> None:
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        exe = resolve_command(self.command)
        if not exe:
            raise MCPError(f"Command not found on PATH: {self.command}")

        env = {**os.environ, **self.env}
        env.setdefault("NO_COLOR", "1")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                exe, *self.args, cwd=self.cwd, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if _WINDOWS else 0,
            )
        except NotImplementedError as exc:   # SelectorEventLoop on Windows
            raise MCPError(
                "This event loop cannot spawn subprocesses. AEGIS sets the "
                "Proactor loop on Windows; something has overridden it."
            ) from exc
        except Exception as exc:
            raise MCPError(f"Could not start {self.command}: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Keep the last few stderr lines - they are what explains a crash."""
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-25]
        except Exception:
            pass

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-8:])

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def send(self, message: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError("Transport is not running")
        data = (json.dumps(message) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def receive(self, timeout: float) -> dict[str, Any]:
        if not self.proc or not self.proc.stdout:
            raise MCPError("Transport is not running")
        while True:
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            except asyncio.TimeoutError as exc:
                raise MCPError(f"No reply within {timeout:.0f}s") from exc
            if not line:
                tail = self.stderr_tail
                raise MCPError(
                    f"Server exited (code {self.proc.returncode})"
                    + (f"\n{tail}" if tail else ""))
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except ValueError:
                # Servers that print banners to stdout are common. Skip noise.
                continue

    async def close(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 6)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        self.proc = None


class HttpTransport:
    """Streamable HTTP. Replies arrive as JSON or as an SSE stream."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self.session_id: str | None = None
        self.client: httpx.AsyncClient | None = None
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def start(self) -> None:
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)

    @property
    def alive(self) -> bool:
        return self.client is not None

    async def send(self, message: dict[str, Any]) -> None:
        if not self.client:
            raise MCPError("Transport is not running")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        resp = await self.client.post(self.url, json=message, headers=headers)
        if sid := resp.headers.get("mcp-session-id"):
            self.session_id = sid
        if resp.status_code == 202:
            return                                    # notification accepted
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for block in resp.text.split("\n\n"):
                for line in block.splitlines():
                    if line.startswith("data:"):
                        try:
                            await self._inbox.put(json.loads(line[5:].strip()))
                        except ValueError:
                            pass
        elif resp.content:
            try:
                payload = resp.json()
            except ValueError as exc:
                raise MCPError(f"Server sent non-JSON: {resp.text[:200]}") from exc
            for item in (payload if isinstance(payload, list) else [payload]):
                await self._inbox.put(item)

    async def receive(self, timeout: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout)
        except asyncio.TimeoutError as exc:
            raise MCPError(f"No reply within {timeout:.0f}s") from exc

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
        self.client = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MCPTool":
        return cls(
            name=payload.get("name", ""),
            description=(payload.get("description") or "").strip(),
            input_schema=payload.get("inputSchema") or {"type": "object",
                                                        "properties": {}},
            annotations=payload.get("annotations") or {},
        )


@dataclass
class ToolCallResult:
    ok: bool
    text: str = ""
    images: list[tuple[str, str]] = field(default_factory=list)  # (media_type, b64)
    raw: Any = None


class MCPClient:
    def __init__(self, name: str, transport: StdioTransport | HttpTransport) -> None:
        self.name = name
        self.transport = transport
        self.tools: list[MCPTool] = []
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.error: str = ""
        self.connected = False
        self._id = 0
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _request(self, method: str, params: dict[str, Any] | None = None,
                       timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """One request/response pair. Serialised, so replies cannot cross."""
        async with self._lock:
            rid = self._next_id()
            await self.transport.send({"jsonrpc": "2.0", "id": rid,
                                       "method": method, "params": params or {}})
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise MCPError(f"{method} timed out after {timeout:.0f}s")
                message = await self.transport.receive(remaining)
                if message.get("id") != rid:
                    continue                       # notification or stale reply
                if err := message.get("error"):
                    raise MCPError(f"{err.get('code', '?')}: "
                                   f"{err.get('message', 'unknown error')}")
                return message.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        async with self._lock:
            await self.transport.send({"jsonrpc": "2.0", "method": method,
                                       "params": params or {}})

    async def connect(self) -> bool:
        try:
            await self.transport.start()
            result = await self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            }, timeout=STARTUP_TIMEOUT)
            self.server_info = result.get("serverInfo") or {}
            self.capabilities = result.get("capabilities") or {}
            await self._notify("notifications/initialized")
            await self.refresh_tools()
            self.connected = True
            self.error = ""
            return True
        except Exception as exc:
            self.error = str(exc)
            self.connected = False
            await self.close()
            return False

    async def refresh_tools(self) -> list[MCPTool]:
        if "tools" not in self.capabilities and self.capabilities:
            self.tools = []
            return self.tools
        result = await self._request("tools/list")
        self.tools = [MCPTool.from_payload(t) for t in result.get("tools", [])]
        return self.tools

    async def call(self, tool: str, arguments: dict[str, Any],
                   timeout: float = DEFAULT_TIMEOUT) -> ToolCallResult:
        try:
            result = await self._request("tools/call",
                                         {"name": tool, "arguments": arguments},
                                         timeout=timeout)
        except MCPError as exc:
            return ToolCallResult(ok=False, text=f"Tool call failed: {exc}")

        texts: list[str] = []
        images: list[tuple[str, str]] = []
        for block in result.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "image":
                images.append((block.get("mimeType", "image/png"),
                               block.get("data", "")))
            elif btype == "resource":
                resource = block.get("resource") or {}
                texts.append(resource.get("text")
                             or f"[resource {resource.get('uri', '')}]")
        if structured := result.get("structuredContent"):
            texts.append(json.dumps(structured, indent=2)[:8000])

        return ToolCallResult(
            ok=not result.get("isError", False),
            text="\n".join(t for t in texts if t).strip() or "(no output)",
            images=images,
            raw=result,
        )

    async def close(self) -> None:
        self.connected = False
        try:
            await self.transport.close()
        except Exception:
            pass


def build_client(name: str, spec: dict[str, Any]) -> MCPClient:
    """Build a client from a server spec (the shape discovery produces)."""
    if url := spec.get("url"):
        headers = dict(spec.get("headers") or {})
        return MCPClient(name, HttpTransport(url, headers))
    command = spec.get("command") or ""
    if not command:
        raise MCPError("Server spec needs either a command or a url")
    return MCPClient(name, StdioTransport(
        command=command,
        args=[str(a) for a in (spec.get("args") or [])],
        env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        cwd=spec.get("cwd") or None,
    ))
