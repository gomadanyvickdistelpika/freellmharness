"""Find MCP servers already configured on this machine.

Reads the config files other MCP hosts write, so a server you set up once in
Codex or Claude Desktop shows up here without retyping it. Nothing is connected
automatically - discovery only proposes; you enable each server yourself.

Config files are other tools' data, not instructions. They are parsed for the
fields below and nothing else, and a malformed file is skipped with a reason
rather than crashing the scan.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:                                  # Python 3.10
    try:
        import tomli as tomllib                      # type: ignore[no-redef]
    except ImportError:
        tomllib = None                               # type: ignore[assignment]


@dataclass
class Discovered:
    name: str
    origin: str                  # codex | claude-desktop | vscode | cursor | manual
    spec: dict[str, Any]         # command/args/env, or url/headers
    source_path: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.origin}:{self.name}"

    @property
    def transport(self) -> str:
        return "http" if self.spec.get("url") else "stdio"

    @property
    def summary(self) -> str:
        if url := self.spec.get("url"):
            return url
        cmd = self.spec.get("command", "")
        args = " ".join(str(a) for a in (self.spec.get("args") or [])[:4])
        return f"{cmd} {args}".strip()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d |= {"key": self.key, "transport": self.transport,
              "summary": self.summary,
              "env_keys": sorted((self.spec.get("env") or {}).keys())}
        # Never ship env *values* to the UI - they are usually API tokens.
        d["spec"] = {k: v for k, v in self.spec.items() if k not in ("env", "headers")}
        return d


def _home() -> Path:
    return Path.home()


def _appdata() -> Path | None:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) if base else None
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (_home() / ".config"))


def _normalise(name: str, raw: Any, origin: str, path: Path) -> Discovered | None:
    """Turn one host's server entry into our shape, or skip it."""
    if not isinstance(raw, dict):
        return None

    note = ""
    if raw.get("disabled") is True or raw.get("enabled") is False:
        note = "disabled in the source config"

    spec: dict[str, Any] = {}
    url = raw.get("url") or raw.get("serverUrl") or raw.get("endpoint")
    if url and isinstance(url, str):
        spec["url"] = url
        if isinstance(raw.get("headers"), dict):
            spec["headers"] = {str(k): str(v) for k, v in raw["headers"].items()}
    else:
        command = raw.get("command")
        if not command or not isinstance(command, str):
            return None
        spec["command"] = command
        args = raw.get("args")
        spec["args"] = [str(a) for a in args] if isinstance(args, list) else []
        env = raw.get("env")
        if isinstance(env, dict):
            spec["env"] = {str(k): str(v) for k, v in env.items()}
        if isinstance(raw.get("cwd"), str):
            spec["cwd"] = raw["cwd"]

    return Discovered(name=str(name), origin=origin, spec=spec,
                      source_path=str(path), note=note)


def from_codex() -> list[Discovered]:
    """~/.codex/config.toml, [mcp_servers.<name>]"""
    path = _home() / ".codex" / "config.toml"
    if not path.exists():
        return []
    if tomllib is None:
        return [Discovered(name="(codex config found)", origin="codex", spec={},
                           source_path=str(path),
                           note="Install tomli to read TOML on Python 3.10")]
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except Exception as exc:
        return [Discovered(name="(unreadable)", origin="codex", spec={},
                           source_path=str(path), note=f"Could not parse: {exc}")]

    servers = data.get("mcp_servers") or data.get("mcpServers") or {}
    out = []
    for name, raw in servers.items():
        if entry := _normalise(name, raw, "codex", path):
            out.append(entry)
    return out


def from_claude_desktop() -> list[Discovered]:
    appdata = _appdata()
    if not appdata:
        return []
    candidates = [appdata / "Claude" / "claude_desktop_config.json"]
    if sys.platform not in ("win32", "darwin"):
        candidates.append(_home() / ".config" / "Claude" / "claude_desktop_config.json")
    return _from_json_hosts(candidates, "claude-desktop", ("mcpServers",))


def from_vscode() -> list[Discovered]:
    appdata = _appdata()
    paths: list[Path] = []
    if appdata:
        for product in ("Code", "Code - Insiders", "VSCodium"):
            paths.append(appdata / product / "User" / "mcp.json")
            paths.append(appdata / product / "User" / "settings.json")
    return _from_json_hosts(paths, "vscode", ("servers", "mcpServers", "mcp.servers"))


def from_cursor() -> list[Discovered]:
    return _from_json_hosts([_home() / ".cursor" / "mcp.json"],
                            "cursor", ("mcpServers", "servers"))


def _from_json_hosts(paths: list[Path], origin: str,
                     keys: tuple[str, ...]) -> list[Discovered]:
    out: list[Discovered] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(_strip_jsonc(path.read_text("utf-8")))
        except Exception as exc:
            out.append(Discovered(name="(unreadable)", origin=origin, spec={},
                                  source_path=str(path),
                                  note=f"Could not parse: {exc}"))
            continue
        if not isinstance(data, dict):
            continue

        servers: dict[str, Any] = {}
        for key in keys:
            node: Any = data
            for part in key.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, dict):
                servers.update(node)
        # VS Code nests under "mcp": {"servers": {...}}
        if isinstance(data.get("mcp"), dict):
            nested = data["mcp"].get("servers")
            if isinstance(nested, dict):
                servers.update(nested)

        for name, raw in servers.items():
            if name in seen:
                continue
            if entry := _normalise(name, raw, origin, path):
                seen.add(name)
                out.append(entry)
    return out


def _strip_jsonc(text: str) -> str:
    """VS Code settings allow // comments and trailing commas. JSON does not."""
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1

    cleaned = "".join(out)
    # Trailing commas before } or ]
    result: list[str] = []
    for idx, ch in enumerate(cleaned):
        if ch == ",":
            rest = cleaned[idx + 1:].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        result.append(ch)
    return "".join(result)


def scan() -> list[Discovered]:
    """Every server this machine already knows about, deduplicated by name."""
    found: list[Discovered] = []
    for source in (from_codex, from_claude_desktop, from_vscode, from_cursor):
        try:
            found.extend(source())
        except Exception as exc:
            found.append(Discovered(name="(scan failed)", origin=source.__name__,
                                    spec={}, note=str(exc)))
    return found


def scan_report() -> dict[str, Any]:
    found = scan()
    origins: dict[str, int] = {}
    for entry in found:
        if entry.spec:
            origins[entry.origin] = origins.get(entry.origin, 0) + 1
    return {
        "servers": [d.to_dict() for d in found],
        "by_origin": origins,
        "paths_checked": {
            "codex": str(_home() / ".codex" / "config.toml"),
            "claude-desktop": str((_appdata() or _home()) / "Claude"
                                  / "claude_desktop_config.json"),
            "vscode": str((_appdata() or _home()) / "Code" / "User" / "mcp.json"),
            "cursor": str(_home() / ".cursor" / "mcp.json"),
        },
    }
