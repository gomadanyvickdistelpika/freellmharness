"""Tool shape shared by MCP tools and built-in tools, plus risk classification.

Classification decides what runs without asking. It follows the rule that
matters most here: **unknown means write**. A tool we cannot confidently call
read-only gets the approval prompt. Being asked about a harmless tool costs a
click; auto-running an unrecognised one can send mail as you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

READ, WRITE = "read", "write"

# Verbs that only observe. Anchored to the start of the tool name or the start
# of a word after a separator, so "get_user" matches and "forget" does not.
_READ_VERBS = (
    "get", "list", "read", "search", "find", "fetch", "query", "describe",
    "show", "view", "inspect", "check", "count", "lookup", "resolve", "status",
    "summarise", "summarize", "diff", "preview", "screenshot", "capture",
    "whoami", "ping", "exists", "head", "stat", "browse", "explain",
)

# Verbs that change something, cost money, or leave the machine.
_WRITE_VERBS = (
    "create", "update", "delete", "remove", "destroy", "drop", "send", "post",
    "put", "patch", "write", "edit", "modify", "set", "add", "insert", "append",
    "move", "rename", "copy", "upload", "download", "install", "uninstall",
    "run", "exec", "execute", "call", "invoke", "deploy", "publish", "merge",
    "commit", "push", "reply", "forward", "trash", "archive", "label",
    "share", "grant", "revoke", "pay", "purchase", "click", "type", "press",
    "drag", "scroll", "key", "kill", "restart", "start", "stop", "clear",
    "empty", "reset", "apply", "approve", "mark", "spam", "untrash",
)

_SPLIT = re.compile(r"[_\-.:/\s]+")


def classify(name: str, annotations: dict[str, Any] | None = None,
             description: str = "") -> str:
    """READ or WRITE. Explicit MCP annotations win; heuristics are the fallback."""
    ann = annotations or {}

    # MCP's own hints are authoritative when present.
    if ann.get("destructiveHint") is True:
        return WRITE
    if ann.get("readOnlyHint") is True:
        return READ
    if ann.get("readOnlyHint") is False:
        return WRITE

    # Split on separators first, keeping case, then split camelCase, then
    # lower. Lowercasing before the camelCase split destroys the boundary and
    # "listMessages" stops being recognisable.
    expanded: list[str] = []
    for word in (w for w in _SPLIT.split(name) if w):
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)   # HTTPGet -> HTTP Get
        expanded.extend(p.lower() for p in spaced.split() if p)

    for word in expanded[:3]:
        if word in _WRITE_VERBS:
            return WRITE
        if word in _READ_VERBS:
            return READ

    # Nothing recognised: fail closed.
    return WRITE


@dataclass
class Tool:
    """One callable tool, whatever its origin."""
    name: str                    # namespaced, e.g. "gmail.search_threads"
    description: str
    input_schema: dict[str, Any]
    server: str                  # owning server, or "computer" / "aegis"
    origin: str                  # "mcp" | "builtin"
    risk: str = WRITE
    raw_name: str = ""           # name as the server knows it
    handler: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "raw_name": self.raw_name,
                "description": self.description[:400], "server": self.server,
                "origin": self.origin, "risk": self.risk,
                "schema": self.input_schema}

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024] or self.name,
                "parameters": _sanitise_schema(self.input_schema),
            },
        }

    def anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description[:1024] or self.name,
            "input_schema": _sanitise_schema(self.input_schema),
        }


def _sanitise_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make sure what we hand a model is a usable JSON-Schema object."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    out = {
        "type": "object",
        "properties": schema.get("properties") or {},
    }
    if isinstance(schema.get("required"), list):
        out["required"] = schema["required"]
    return out


def namespace(server: str, tool: str) -> str:
    """Tool names must be unique across servers and match ^[a-zA-Z0-9_-]{1,64}$."""
    safe_server = re.sub(r"[^A-Za-z0-9_-]", "_", server)[:24].strip("_")
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool)[:38].strip("_")
    return f"{safe_server}__{safe_tool}"
