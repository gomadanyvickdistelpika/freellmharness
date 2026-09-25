"""File tools, fenced into the folders you connect.

Reading is free; writing asks. But the fence comes first: every path is resolved
to its real location on disk and checked against the connected roots before
anything opens it. That resolution is what makes it a fence rather than a
suggestion - `..\\..\\Windows\\System32`, a symlink pointing at your SSH keys and
an absolute path to somewhere else entirely all collapse to a real path, and a
real path either sits under a connected root or it does not.

Nothing outside a connected folder is reachable by any of these tools. Connect
nothing and the agent has no filesystem at all.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from ..config import settings
from .base import READ, WRITE, Tool

MAX_READ = 200_000          # characters returned from one file
MAX_LIST = 400              # entries in one listing
MAX_HITS = 80               # search results
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".idea", ".vscode", "$RECYCLE.BIN", "System Volume Information"}


# ---------------------------------------------------------------------------
# Connected folders
# ---------------------------------------------------------------------------

def folders() -> list[str]:
    return [str(p) for p in (settings.get("folders") or [])]


def roots() -> list[Path]:
    """Where file tools may go, in the order relative paths are tried.

    v2 adds two roots the agent owns, like Claude's own sandbox:
      * the current project's workspace, first, so "notes.md" means the
        project's notes while you are in a project;
      * the general AEGIS workspace, last, so there is always somewhere to
        build and save things even with nothing connected.
    Turn the general one off with Settings -> workspace_enabled.
    """
    out: list[Path] = []
    try:
        from .. import projects
        key = projects.current_key()
        if key and projects.get(key):
            out.append(projects.workspace(key).resolve(strict=False))
    except Exception:
        pass
    for entry in folders():
        try:
            resolved = Path(entry).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        out.append(resolved)
    if settings.get("workspace_enabled", True):
        try:
            from .. import projects
            out.append(projects.general_workspace().resolve(strict=False))
        except Exception:
            pass
        try:
            # Your uploads, so the tools can work on a huge file in place.
            from ..files import ATTACH_DIR
            out.append(ATTACH_DIR.resolve(strict=False))
        except Exception:
            pass
    return out


def add_folder(path: str) -> dict[str, Any]:
    candidate = Path(str(path).strip().strip('"')).expanduser()
    if not candidate.is_dir():
        return {"ok": False, "error": f"{candidate} is not a folder."}
    resolved = str(candidate.resolve(strict=False))
    current = folders()
    if resolved in current:
        return {"ok": False, "error": "That folder is already connected."}
    settings.set("folders", [*current, resolved])
    return {"ok": True, "path": resolved, "folders": folders()}


def remove_folder(path: str) -> dict[str, Any]:
    keep = [f for f in folders() if f != str(path)]
    settings.set("folders", keep)
    return {"ok": True, "folders": keep}


class OutsideFence(Exception):
    """Raised when a path resolves outside every connected folder."""


def resolve(raw: str) -> Path:
    """Resolve a path and prove it lands inside a connected folder.

    resolve() follows symlinks and flattens '..', so this is checked against
    where the path actually ends up, not what it looks like.
    """
    available = roots()
    if not available:
        raise OutsideFence("No folders are connected. Connect one on the Files "
                           "tab first.")

    text = str(raw or "").strip().strip('"')
    if not text:
        raise OutsideFence("A path is required.")

    # Treat a backslash as a separator everywhere, not just on Windows. On Linux
    # it is a legal filename character, so "..\\secret.txt" would otherwise be
    # one oddly-named file *inside* the fence here and a real traversal on the
    # machine this actually runs on. Normalise to the stricter reading.
    text = text.replace("\\", "/")

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        # A relative path is taken against the first connected folder.
        candidate = available[0] / candidate

    try:
        target = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise OutsideFence(f"Could not resolve that path: {exc}") from exc

    for root in available:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue

    raise OutsideFence(
        f"{target} is outside every connected folder. Connected: "
        f"{', '.join(str(r) for r in available)}")


def _is_texty(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(4096)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _folders(**_: Any) -> dict[str, Any]:
    available = [str(r) for r in roots()]
    if not available:
        return {"text": "No folders are connected, so there is no filesystem "
                        "access at all. The user connects one on the Files tab."}
    return {"text": "Connected folders (nothing outside these is reachable):\n"
                    + "\n".join(f"  {f}" for f in available)}


def _list(path: str = "", pattern: str = "", **_: Any) -> dict[str, Any]:
    if not path:
        available = folders()
        if not available:
            return _folders()
        path = available[0]
    target = resolve(path)
    if not target.is_dir():
        return {"text": f"{target} is not a folder.", "error": True}

    rows: list[str] = []
    try:
        entries = sorted(target.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return {"text": f"Could not list {target}: {exc}", "error": True}

    for entry in entries[:MAX_LIST]:
        if entry.name in SKIP_DIRS:
            continue
        if pattern and not entry.is_dir() and not fnmatch.fnmatch(entry.name, pattern):
            continue
        try:
            if entry.is_dir():
                rows.append(f"  [dir]  {entry.name}/")
            else:
                size = entry.stat().st_size
                rows.append(f"  {size:>10,}  {entry.name}")
        except OSError:
            continue

    more = "" if len(entries) <= MAX_LIST else f"\n  ... {len(entries) - MAX_LIST} more"
    return {"text": f"{target}\n" + ("\n".join(rows) or "  (empty)") + more}


def _read(path: str = "", offset: int = 0, limit: int = MAX_READ,
          **_: Any) -> dict[str, Any]:
    target = resolve(path)
    if not target.is_file():
        return {"text": f"{target} is not a file.", "error": True}
    if not _is_texty(target):
        return {"text": f"{target.name} is binary; it cannot be read as text.",
                "error": True}
    try:
        content = target.read_text("utf-8", errors="replace")
    except OSError as exc:
        return {"text": f"Could not read {target}: {exc}", "error": True}

    offset = max(0, int(offset))
    limit = min(int(limit or MAX_READ), MAX_READ)
    chunk = content[offset:offset + limit]
    tail = ("" if offset + len(chunk) >= len(content)
            else f"\n\n[{len(content) - offset - len(chunk):,} more characters; "
                 f"read again with offset={offset + len(chunk)}]")
    return {"text": f"--- {target} ---\n{chunk}{tail}"}


def _search(query: str = "", path: str = "", pattern: str = "*",
            **_: Any) -> dict[str, Any]:
    available = [resolve(path)] if path else roots()
    if not available:
        return _folders()
    try:
        needle = re.compile(query, re.IGNORECASE)
    except re.error:
        needle = re.compile(re.escape(query), re.IGNORECASE)

    hits: list[str] = []
    scanned = 0
    for root in available:
        for current, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                if not fnmatch.fnmatch(name, pattern or "*"):
                    continue
                file = Path(current) / name
                try:
                    if file.stat().st_size > 5_000_000 or not _is_texty(file):
                        continue
                    scanned += 1
                    for number, line in enumerate(
                            file.read_text("utf-8", errors="replace").splitlines(), 1):
                        if needle.search(line):
                            hits.append(f"{file}:{number}: {line.strip()[:200]}")
                            if len(hits) >= MAX_HITS:
                                break
                except OSError:
                    continue
                if len(hits) >= MAX_HITS:
                    break
            if len(hits) >= MAX_HITS:
                break

    if not hits:
        return {"text": f"No match for {query!r} in {scanned} file(s)."}
    capped = "\n[stopped at the result limit]" if len(hits) >= MAX_HITS else ""
    return {"text": f"{len(hits)} match(es) in {scanned} file(s):\n"
                    + "\n".join(hits) + capped}


# ---------------------------------------------------------------------------
# v2.1: checkpoints (undo) and diff previews - the Codex/Claude Code safety net
# ---------------------------------------------------------------------------

def _checkpoint_dir() -> Path:
    from ..config import DATA_DIR
    folder = DATA_DIR / "checkpoints"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def checkpoint(target: Path) -> str:
    """Save the current state of a file before it changes. Returns an id."""
    import json
    import time
    import uuid
    cid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    folder = _checkpoint_dir()
    existed = target.is_file()
    if existed:
        (folder / f"{cid}.bak").write_bytes(target.read_bytes())
    try:
        from .. import projects
        chat = projects.current_chat()
    except Exception:
        chat = ""
    with open(folder / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": cid, "path": str(target), "existed": existed,
                             "time": time.time(), "chat": chat}) + "\n")
    return cid


def checkpoints(limit: int = 100) -> list[dict[str, Any]]:
    import json
    index = _checkpoint_dir() / "index.jsonl"
    if not index.is_file():
        return []
    rows = []
    for line in index.read_text("utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))[:limit]


def restore(cid: str = "") -> dict[str, Any]:
    rows = checkpoints(1000)
    row = next((r for r in rows if r["id"] == cid), None) if cid else (rows[0] if rows else None)
    if not row:
        return {"ok": False, "error": "No such checkpoint."}
    target = Path(row["path"])
    backup = _checkpoint_dir() / f"{row['id']}.bak"
    checkpoint(target)                          # the undo is undoable too
    if row["existed"] and backup.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(backup.read_bytes())
        return {"ok": True, "text": f"Restored {target} to how it was before "
                                    f"checkpoint {row['id']}."}
    if target.exists():
        target.unlink()
    return {"ok": True, "text": f"{target} did not exist before; removed it."}


def preview(tool_name: str, args: dict[str, Any]) -> str:
    """A unified diff of what a write/edit would change, for the approval card."""
    import difflib
    try:
        target = resolve(str(args.get("path") or ""))
    except OutsideFence as exc:
        return f"(outside the fence: {exc})"
    old = target.read_text("utf-8", errors="replace") if target.is_file() else ""
    if tool_name.endswith("edit"):
        snippet = str(args.get("old") or "")
        if not snippet or snippet not in old:
            return f"{target}: the text to replace was not found"
        new = old.replace(snippet, str(args.get("new") or ""),
                          -1 if args.get("replace_all") else 1)
    elif args.get("append"):
        new = old + str(args.get("content") or "")
    else:
        new = str(args.get("content") or "")
    label = "new file" if not target.exists() else "change"
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{target.name} (now)", tofile=f"{target.name} ({label})", n=2))
    if len(diff) > 6000:
        diff = diff[:6000] + "\n… (diff truncated)"
    return f"{target}\n{diff or '(no change)'}"


def _undo(id: str = "", **_: Any) -> dict[str, Any]:
    result = restore(id)
    return {"text": result.get("text") or result.get("error"), "error": not result["ok"]}


def _write(path: str = "", content: str = "", append: bool = False,
           **_: Any) -> dict[str, Any]:
    target = resolve(path)
    if target.is_dir():
        return {"text": f"{target} is a folder.", "error": True}
    cid = checkpoint(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if append else "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(content)
    except OSError as exc:
        return {"text": f"Could not write {target}: {exc}", "error": True}
    verb = "Appended to" if append else "Wrote"
    return {"text": f"{verb} {target} ({len(content):,} characters). "
                    f"Checkpoint {cid} (files__undo reverts it). [[file:{target}]]"}


def _edit(path: str = "", old: str = "", new: str = "", replace_all: bool = False,
          **_: Any) -> dict[str, Any]:
    """Exact-text replacement - the safe way to change part of a file."""
    target = resolve(path)
    if not target.is_file():
        return {"text": f"{target} does not exist. Use files__write to create it.",
                "error": True}
    text = target.read_text("utf-8", errors="replace")
    if not old:
        return {"text": "old must be the exact text to replace.", "error": True}
    count = text.count(old)
    if count == 0:
        return {"text": "The old text was not found. Read the file again and copy "
                        "the exact lines, including indentation.", "error": True}
    if count > 1 and not replace_all:
        return {"text": f"The old text appears {count} times. Include more "
                        "surrounding lines to make it unique, or set "
                        "replace_all.", "error": True}
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    checkpoint(target)
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated)
    import difflib
    diff = "".join(difflib.unified_diff(
        text.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=target.name, tofile=target.name, n=2))
    if len(diff) > 3000:
        diff = diff[:3000] + "\n…"
    return {"text": f"Edited {target} ({count if replace_all else 1} "
                    f"replacement{'s' if replace_all and count > 1 else ''}).\n{diff}"}


# -- attachments -------------------------------------------------------------

def _search_attachments(query: str = "", chat_id: str = "",
                        **_: Any) -> dict[str, Any]:
    from .. import files as files_mod

    hits = files_mod.search(query, chat_id)
    if not hits:
        return {"text": f"Nothing in the attached files matches {query!r}."}
    lines = [f"[{h['attach_id']}] {h['name']}\n    {h['snip']}" for h in hits]
    return {"text": "\n".join(lines)
            + "\n\nRead one in full with files__read_attachment."}


def _read_attachment(id: str = "", offset: int = 0, limit: int = 8000,
                     **_: Any) -> dict[str, Any]:
    from .. import files as files_mod

    result = files_mod.text_of(str(id), int(offset or 0), int(limit or 8000))
    if not result.get("ok"):
        return {"text": result.get("error", "Not found."), "error": True}
    if not result.get("text"):
        return {"text": result.get("note", "That file has no text.")}
    tail = ("" if not result.get("more")
            else f"\n\n[more remains; read again with offset="
                 f"{result['offset'] + result['returned']}]")
    return {"text": f"--- {result['name']} ---\n{result['text']}{tail}"}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _wrap(fn):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, **args)
        except OutsideFence as exc:
            return {"text": f"Refused: {exc}", "error": True}
        except Exception as exc:
            return {"text": f"{type(exc).__name__}: {exc}", "error": True}
    return handler


def tools() -> list[Tool]:
    def tool(name, desc, schema, risk, fn) -> Tool:
        return Tool(name=f"files__{name}", raw_name=name, description=desc,
                    input_schema=schema, server="files", origin="builtin",
                    risk=risk, handler=_wrap(fn))

    has_folders = bool(roots())
    built: list[Tool] = [
        tool("search_attachments",
             "Search inside files attached to this conversation. Large "
             "attachments are not in the prompt - this is how you find the "
             "parts that matter.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "chat_id": {"type": "string"}},
              "required": ["query"]},
             READ, _search_attachments),

        tool("read_attachment",
             "Read an attached file by its id, a chunk at a time.",
             {"type": "object",
              "properties": {"id": {"type": "string"},
                             "offset": {"type": "integer"},
                             "limit": {"type": "integer"}},
              "required": ["id"]},
             READ, _read_attachment),
    ]

    if not has_folders:
        return built

    built += [
        tool("folders", "List the folders you are allowed to touch.",
             {"type": "object", "properties": {}}, READ, _folders),

        tool("list", "List a folder. Defaults to the first connected one.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "pattern": {"type": "string",
                                         "description": "Glob, e.g. *.pdf"}}},
             READ, _list),

        tool("read", "Read a text file from a connected folder.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "offset": {"type": "integer"},
                             "limit": {"type": "integer"}},
              "required": ["path"]},
             READ, _read),

        tool("search",
             "Search file contents across connected folders. Accepts a regular "
             "expression; falls back to a literal search if it will not compile.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "path": {"type": "string"},
                             "pattern": {"type": "string",
                                         "description": "Filename glob, e.g. *.md"}},
              "required": ["query"]},
             READ, _search),

        tool("write",
             "Create or overwrite a file inside a connected folder. Set append "
             "to add to the end instead.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "content": {"type": "string"},
                             "append": {"type": "boolean"}},
              "required": ["path", "content"]},
             WRITE, _write),

        tool("edit",
             "Change part of a text file by replacing an exact snippet with new "
             "text. Read the file first; copy the old text exactly. Safer than "
             "rewriting the whole file.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "old": {"type": "string"},
                             "new": {"type": "string"},
                             "replace_all": {"type": "boolean"}},
              "required": ["path", "old", "new"]},
             WRITE, _edit),

        tool("undo",
             "Undo a file change made by files__write or files__edit: restores "
             "the file from its checkpoint. Without an id, undoes the latest.",
             {"type": "object", "properties": {"id": {"type": "string"}}},
             WRITE, _undo),
    ]
    return built


def status() -> dict[str, Any]:
    from .. import files as files_mod

    return {"folders": folders(), "tool_count": len(tools()),
            **files_mod.stats()}
