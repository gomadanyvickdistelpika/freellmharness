"""One-click MCP servers.

A short, curated list of well-known MCP servers with the exact command each
needs, which fields you must fill in (a folder, a token), and what has to be
installed first (Node for npx, uv for uvx). Installing one adds it to AEGIS's
own MCP config and connects it - the tools appear straight away.

Nothing here runs until you press Install, and anything with a secret stores
that secret only in the server's env inside settings (never in a prompt).
The agent can also install from this list (mcp_admin__install), which asks
you first, like any other write.
"""

from __future__ import annotations

import shutil
from typing import Any

CATALOG: list[dict[str, Any]] = [
    {"key": "filesystem", "label": "Filesystem",
     "description": "Read/write files in one folder you choose (official server).",
     "needs": "node",
     "spec": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "{folder}"]},
     "fields": [{"name": "folder", "label": "Folder", "placeholder": "C:\\Users\\you\\Documents"}]},
    {"key": "fetch", "label": "Fetch (web pages → Markdown)",
     "description": "Fetch any URL and convert it to Markdown (official server).",
     "needs": "uv", "spec": {"command": "uvx", "args": ["mcp-server-fetch"]}, "fields": []},
    {"key": "git", "label": "Git",
     "description": "Inspect and work with a local git repository (official server).",
     "needs": "uv",
     "spec": {"command": "uvx", "args": ["mcp-server-git", "--repository", "{repo}"]},
     "fields": [{"name": "repo", "label": "Repository folder", "placeholder": "C:\\Users\\you\\code\\my-repo"}]},
    {"key": "memory-graph", "label": "Memory (knowledge graph)",
     "description": "A persistent knowledge graph the agent can write to (official server).",
     "needs": "node",
     "spec": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}, "fields": []},
    {"key": "sequential-thinking", "label": "Sequential thinking",
     "description": "Structured step-by-step reasoning tool — helps small models on hard problems.",
     "needs": "node",
     "spec": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]},
     "fields": []},
    {"key": "time", "label": "Time & timezones",
     "description": "Current time and timezone conversion (official server).",
     "needs": "uv", "spec": {"command": "uvx", "args": ["mcp-server-time"]}, "fields": []},
    {"key": "playwright", "label": "Playwright browser (Microsoft)",
     "description": "A second, accessibility-tree browser automation server.",
     "needs": "node",
     "spec": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}, "fields": []},
    {"key": "github", "label": "GitHub (official, remote)",
     "description": "Repos, issues, pull requests, code search — needs a GitHub personal access token.",
     "needs": "",
     "spec": {"url": "https://api.githubcopilot.com/mcp/",
              "headers": {"Authorization": "Bearer {token}"}},
     "fields": [{"name": "token", "label": "GitHub token", "secret": True,
                 "placeholder": "github_pat_…"}]},
    {"key": "google-workspace", "label": "Google Workspace (Gmail, Drive, Calendar, Docs)",
     "description": "taylorwilsdon/google_workspace_mcp — needs a Google OAuth client id/secret "
                    "(you already have ~/.google_workspace_mcp from Codex).",
     "needs": "uv",
     "spec": {"command": "uvx", "args": ["workspace-mcp"],
              "env": {"GOOGLE_OAUTH_CLIENT_ID": "{client_id}",
                      "GOOGLE_OAUTH_CLIENT_SECRET": "{client_secret}",
                      "USER_GOOGLE_EMAIL": "{email}"}},
     "fields": [{"name": "client_id", "label": "OAuth client id"},
                {"name": "client_secret", "label": "OAuth client secret", "secret": True},
                {"name": "email", "label": "Your Google email", "placeholder": "you@gmail.com"}]},
    {"key": "brave-search", "label": "Brave Search",
     "description": "Web, news and image search through Brave's API (free tier available).",
     "needs": "node",
     "spec": {"command": "npx", "args": ["-y", "@brave/brave-search-mcp-server"],
              "env": {"BRAVE_API_KEY": "{key}"}},
     "fields": [{"name": "key", "label": "Brave API key", "secret": True}]},
    {"key": "sqlite", "label": "SQLite",
     "description": "Query and edit a local SQLite database.",
     "needs": "uv",
     "spec": {"command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "{db}"]},
     "fields": [{"name": "db", "label": "Database file", "placeholder": "C:\\data\\notes.db"}]},
]

NEEDS_HINT = {
    "node": "Node.js is needed (npx). Install: winget install OpenJS.NodeJS.LTS",
    "uv": "uv is needed (uvx). Install: pip install uv   (or: winget install astral-sh.uv)",
}


def _have(tool: str) -> bool:
    for name in (tool, tool + ".cmd", tool + ".exe"):
        if shutil.which(name):
            return True
    return False


def prerequisites() -> dict[str, bool]:
    return {"node": _have("npx"), "uv": _have("uvx")}


def listing() -> list[dict[str, Any]]:
    from .registry import registry
    have = prerequisites()
    installed = set(registry.configured())
    out = []
    for item in CATALOG:
        need = item.get("needs") or ""
        out.append({**item, "ready": not need or have.get(need, False),
                    "hint": "" if not need or have.get(need) else NEEDS_HINT[need],
                    "installed": item["key"] in installed})
    return out


def _fill(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    return value


async def install(key: str, values: dict[str, str] | None = None,
                  connect: bool = True) -> dict[str, Any]:
    from .registry import registry
    item = next((c for c in CATALOG if c["key"] == key), None)
    if not item:
        return {"ok": False, "error": f"No catalogue entry {key!r}."}
    values = {k: str(v).strip() for k, v in (values or {}).items()}
    missing = [f["label"] for f in item.get("fields", []) if not values.get(f["name"])]
    if missing:
        return {"ok": False, "error": "Please fill in: " + ", ".join(missing)}
    spec = _fill(item["spec"], values)
    registry.add(key, spec, origin="catalog", enabled=False)
    result: dict[str, Any] = {"ok": True, "key": key, "connected": False}
    if connect:
        need = item.get("needs") or ""
        if need and not prerequisites().get(need):
            result["note"] = NEEDS_HINT[need] + " — then press Connect."
            return result
        status = await registry.enable(key)
        result["connected"] = bool(status.get("ok", status.get("connected", False)))
        result["status"] = status
    return result


def add_custom(name: str, command: str = "", args: list[str] | None = None,
               env: dict[str, str] | None = None, url: str = "",
               headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Register any MCP server - including one the agent just wrote."""
    from .registry import registry
    import re
    name = re.sub(r"[^a-z0-9_-]", "-", name.strip().lower())[:32].strip("-")
    if not name or not (command or url):
        return {"ok": False, "error": "A name and a command (or url) are required."}
    spec: dict[str, Any] = {"url": url, "headers": headers or {}} if url else \
        {"command": command, "args": list(args or []), "env": dict(env or {})}
    registry.add(name, spec, origin="agent", enabled=False)
    return {"ok": True, "name": name, "spec": spec}


def tools() -> list[Any]:
    from ..tools.base import READ, WRITE, Tool

    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        lines = [f"- {i['key']}: {i['label']} — {i['description']}"
                 f"{' [installed]' if i['installed'] else ''}"
                 f"{'' if i['ready'] else ' (needs ' + i['needs'] + ')'}"
                 + (f" fields: {', '.join(f['name'] for f in i['fields'])}" if i['fields'] else "")
                 for i in listing()]
        return {"text": "MCP catalogue:\n" + "\n".join(lines)}

    async def _install(args: dict[str, Any]) -> dict[str, Any]:
        result = await install(str(args.get("key") or ""), args.get("values") or {})
        if not result.get("ok"):
            return {"text": result.get("error", "failed"), "error": True}
        return {"text": f"Installed {result['key']}. "
                        + ("Connected — its tools are available from the next message."
                           if result.get("connected") else result.get("note", "Added; connect it on the Tools & MCP tab."))}

    async def _register(args: dict[str, Any]) -> dict[str, Any]:
        result = add_custom(str(args.get("name") or ""), str(args.get("command") or ""),
                            [str(a) for a in (args.get("args") or [])],
                            {str(k): str(v) for k, v in (args.get("env") or {}).items()},
                            str(args.get("url") or ""))
        if not result.get("ok"):
            return {"text": result["error"], "error": True}
        return {"text": f"Registered MCP server '{result['name']}' (disabled). The user "
                        f"can connect it on the Tools & MCP tab, or call "
                        f"mcp_admin__connect."}

    async def _connect(args: dict[str, Any]) -> dict[str, Any]:
        from .registry import registry
        status = await registry.enable(str(args.get("name") or ""))
        return {"text": str(status)[:1500]}

    def t(name, desc, props, required, risk, fn) -> Tool:
        return Tool(name=f"mcp_admin__{name}", raw_name=name, description=desc,
                    input_schema={"type": "object", "properties": props, "required": required},
                    server="mcp_admin", origin="builtin", risk=risk, handler=fn)

    return [
        t("catalog", "List ready-made MCP servers AEGIS can install (GitHub, Google "
          "Workspace, filesystem, fetch, git, Playwright, Brave, SQLite…).",
          {}, [], READ, _list),
        t("install", "Install and connect an MCP server from the catalogue. Asks first.",
          {"key": {"type": "string"},
           "values": {"type": "object", "description": "the entry's fields, e.g. {\"folder\": \"C:\\\\…\"}"}},
          ["key"], WRITE, _install),
        t("register", "Register a custom MCP server (e.g. one you just wrote in the "
          "workspace with FastMCP: command 'python', args ['C:\\\\…\\\\server.py']). Asks first.",
          {"name": {"type": "string"}, "command": {"type": "string"},
           "args": {"type": "array", "items": {"type": "string"}},
           "env": {"type": "object"}, "url": {"type": "string"}},
          ["name"], WRITE, _register),
        t("connect", "Connect (start) a registered MCP server. Asks first.",
          {"name": {"type": "string"}}, ["name"], WRITE, _connect),
    ]
