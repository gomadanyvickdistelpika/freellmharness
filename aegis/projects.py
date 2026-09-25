"""Projects: a workspace, standing instructions and a set of chats that belong
together - the way Claude's Projects work, but on your own disk.

A project is:

    name, instructions   what every chat in it should know and how to behave
    folder               its workspace (default %LOCALAPPDATA%\\Aegis\\projects\\<key>),
                         or any folder you point it at, e.g. a git repo
    PROJECT.md           living notes in the workspace. Read into every chat in
                         the project, and the agent may update it - it is the
                         project's memory ("we chose FastAPI", "deadline Friday")
    knowledge/           drop reference files here; their names are listed in
                         every chat and the agent reads them on demand
    route, skills        optional: a route to use, and skills to preload
    schedules            scheduled tasks can run inside a project

The current project travels with a run as a context variable, so tools (code,
files, media) know which workspace they are in without every call having to
say so.
"""

from __future__ import annotations

import contextvars
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, settings

PROJECTS_DIR = DATA_DIR / "projects"
GENERAL_WORKSPACE = DATA_DIR / "workspace"
NOTES_FILE = "PROJECT.md"
KNOWLEDGE_DIR = "knowledge"
MAX_NOTES_CHARS = 8000
MAX_KNOWLEDGE_LIST = 60

_current_project: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aegis_project", default="")
_current_chat: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aegis_chat", default="")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def set_current(project: str, chat_id: str = "") -> None:
    _current_project.set(project or "")
    _current_chat.set(chat_id or "")


def current_key() -> str:
    return _current_project.get()


def current_chat() -> str:
    return _current_chat.get()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.strip().lower())[:40].strip("-")


def all_projects() -> dict[str, dict[str, Any]]:
    return dict(settings.get("projects") or {})


def get(key: str) -> dict[str, Any] | None:
    item = all_projects().get(key)
    return {**item, "key": key} if item else None


def workspace(key: str) -> Path:
    item = all_projects().get(key) or {}
    folder = Path(item.get("folder") or (PROJECTS_DIR / key)).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def general_workspace() -> Path:
    GENERAL_WORKSPACE.mkdir(parents=True, exist_ok=True)
    return GENERAL_WORKSPACE


def save(name: str, *, key: str = "", instructions: str = "", folder: str = "",
         route: str = "", skills: list[str] | None = None,
         description: str = "") -> dict[str, Any]:
    name = name.strip()
    if not name:
        return {"ok": False, "error": "A project needs a name."}
    stored = all_projects()
    if not key:
        key = _slug(name) or f"p{int(time.time())}"
        if key in stored:                   # a new project never overwrites
            key = f"{key}-{int(time.time()) % 10000}"
    if folder:
        path = Path(folder).expanduser()
        if not path.is_dir():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"Cannot use {folder}: {exc}"}
        folder = str(path.resolve())
    previous = stored.get(key) or {}
    stored[key] = {
        "name": name[:80],
        "description": description.strip()[:400] or previous.get("description", ""),
        "instructions": instructions.strip()[:20000],
        "folder": folder or previous.get("folder", ""),
        "route": route,
        "skills": [s for s in (skills or []) if s][:8],
        "created": previous.get("created") or time.time(),
        "updated": time.time(),
    }
    settings.set("projects", stored)
    ws = workspace(key)
    (ws / KNOWLEDGE_DIR).mkdir(exist_ok=True)
    notes = ws / NOTES_FILE
    if not notes.exists():
        notes.write_text(f"# {name}\n\n## Goal\n\n{description.strip() or '(write the goal here)'}"
                         f"\n\n## Decisions\n\n## Next steps\n", encoding="utf-8")
    return {"ok": True, "key": key, "project": summary_one(key)}


def delete(key: str) -> bool:
    """Forget the project. Its folder and files are left on disk on purpose."""
    stored = all_projects()
    existed = key in stored
    stored.pop(key, None)
    settings.set("projects", stored)
    conn = _db()
    with _lock:
        conn.execute("DELETE FROM chat_projects WHERE project = ?", (key,))
        conn.commit()
    return existed


# ---------------------------------------------------------------------------
# Chats in projects - a side table next to the chat store
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    from . import store
    conn = store.connect()
    with _lock:
        conn.execute("CREATE TABLE IF NOT EXISTS chat_projects ("
                     "chat_id TEXT PRIMARY KEY, project TEXT NOT NULL)")
        conn.commit()
    return conn


def assign(chat_id: str, project: str) -> None:
    conn = _db()
    with _lock:
        if project:
            conn.execute("INSERT OR REPLACE INTO chat_projects VALUES (?, ?)",
                         (chat_id, project))
        else:
            conn.execute("DELETE FROM chat_projects WHERE chat_id = ?", (chat_id,))
        conn.commit()


def project_of(chat_id: str) -> str:
    if not chat_id:
        return ""
    conn = _db()
    with _lock:
        row = conn.execute("SELECT project FROM chat_projects WHERE chat_id = ?",
                           (chat_id,)).fetchone()
    return row[0] if row else ""


def chats_in(project: str) -> list[str]:
    conn = _db()
    with _lock:
        rows = conn.execute("SELECT chat_id FROM chat_projects WHERE project = ?",
                            (project,)).fetchall()
    return [r[0] for r in rows]


def chat_map() -> dict[str, str]:
    conn = _db()
    with _lock:
        rows = conn.execute("SELECT chat_id, project FROM chat_projects").fetchall()
    return {a: b for a, b in rows}


# ---------------------------------------------------------------------------
# What a chat in this project is told
# ---------------------------------------------------------------------------

def knowledge_files(key: str) -> list[str]:
    folder = workspace(key) / KNOWLEDGE_DIR
    if not folder.is_dir():
        return []
    names = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            names.append(str(path.relative_to(workspace(key))).replace("\\", "/"))
        if len(names) >= MAX_KNOWLEDGE_LIST:
            break
    return names


def system_block(key: str) -> str:
    item = get(key)
    if not item:
        return ""
    ws = workspace(key)
    lines = [f"# Project: {item['name']}"]
    if item.get("description"):
        lines.append(item["description"])
    lines.append(f"Workspace folder: {ws}  (files__ and code__ tools default "
                 f"here; save deliverables here).")
    if item.get("instructions"):
        lines += ["", "## Project instructions", item["instructions"]]
    notes = ws / NOTES_FILE
    if notes.is_file():
        text = notes.read_text("utf-8", errors="replace")
        if len(text) > MAX_NOTES_CHARS:
            text = text[:MAX_NOTES_CHARS] + "\n…(truncated - read the file for the rest)"
        lines += ["", f"## {NOTES_FILE} (project memory - update it with "
                      f"files__edit when decisions or next steps change)", text]
    for rules in ("AGENTS.md", "CLAUDE.md", ".aegis/rules.md"):
        rpath = ws / rules
        if rpath.is_file():
            text = rpath.read_text("utf-8", errors="replace")[:6000]
            lines += ["", f"## Repository rules ({rules}) — follow them", text]
    files = knowledge_files(key)
    if files:
        lines += ["", "## Knowledge files (read with files__read when relevant)",
                  *[f"- {f}" for f in files]]
    return "\n".join(lines)


def summary_one(key: str) -> dict[str, Any]:
    item = get(key) or {}
    return {**item, "workspace": str(workspace(key)) if item else "",
            "knowledge": knowledge_files(key) if item else [],
            "chats": len(chats_in(key)) if item else 0}


def summary() -> dict[str, Any]:
    return {"projects": sorted((summary_one(k) for k in all_projects()),
                               key=lambda p: -(p.get("updated") or 0)),
            "general_workspace": str(general_workspace())}


# ---------------------------------------------------------------------------
# Tools: the agent can create projects and schedules from a chat
# ---------------------------------------------------------------------------

def tools() -> list[Any]:
    from . import scheduler
    from .tools.base import READ, WRITE, Tool

    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        result = save(str(args.get("name") or ""),
                      instructions=str(args.get("instructions") or ""),
                      description=str(args.get("description") or ""),
                      folder=str(args.get("folder") or ""))
        if not result.get("ok"):
            return {"text": result.get("error", "failed"), "error": True}
        chat = current_chat()
        if chat and args.get("move_this_chat", True):
            assign(chat, result["key"])
        p = result["project"]
        return {"text": f"Project '{p['name']}' created (key {result['key']}). "
                        f"Workspace: {p['workspace']}."
                        + (" This chat is now in it." if chat else "")}

    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        items = summary()["projects"]
        if not items:
            return {"text": "No projects yet."}
        return {"text": "\n".join(f"- {p['key']}: {p['name']} — {p['chats']} "
                                  f"chats — {p['workspace']}" for p in items)}

    async def _schedule(args: dict[str, Any]) -> dict[str, Any]:
        cron = str(args.get("cron") or "").strip()
        result = scheduler.create(
            str(args.get("name") or ""), str(args.get("prompt") or ""),
            cron or "0 8 * * 1-5",
            project=str(args.get("project") or current_key()),
            provider=str(args.get("provider") or ""),
            trusted=False)
        if not result.get("ok"):
            return {"text": result.get("error", "failed"), "error": True}
        t = result["task"]
        return {"text": f"Scheduled '{t['name']}' ({t['schedule_text']}); next "
                        f"run {t['next_run_text']}. It runs unattended, so "
                        f"anything needing approval is declined unless you mark "
                        f"it trusted on the Files & Tasks tab. To run it while "
                        f"AEGIS is closed, register it with Windows there."}

    async def _schedules(args: dict[str, Any]) -> dict[str, Any]:
        items = scheduler.summary().get("tasks", [])
        if not items:
            return {"text": "No scheduled tasks."}
        return {"text": "\n".join(
            f"- {t['key']}: {t['name']} — {t['schedule_text']} — next "
            f"{t['next_run_text']} — last {t.get('last_status') or 'never'}"
            for t in items)}

    def t(name, desc, props, required, risk, fn) -> Tool:
        return Tool(name=f"project__{name}", raw_name=name, description=desc,
                    input_schema={"type": "object", "properties": props,
                                  "required": required},
                    server="project", origin="builtin", risk=risk, handler=fn)

    return [
        t("create", "Create a project (workspace + instructions + notes) and "
          "move this chat into it. Use when the user starts something that "
          "will span several sessions.",
          {"name": {"type": "string"}, "description": {"type": "string"},
           "instructions": {"type": "string",
                            "description": "standing instructions for every chat"},
           "folder": {"type": "string",
                      "description": "optional existing folder, e.g. a repo"},
           "move_this_chat": {"type": "boolean"}},
          ["name"], WRITE, _create),
        t("list", "List projects and their workspaces.", {}, [], READ, _list),
        t("schedule", "Create a scheduled task that runs a prompt on a cron "
          "schedule (e.g. '0 8 * * 1-5' = weekdays 08:00), optionally inside "
          "a project. Use for daily job searches, reminders, reports.",
          {"name": {"type": "string"}, "prompt": {"type": "string",
                                                  "description": "complete, standalone instruction"},
           "cron": {"type": "string"}, "project": {"type": "string"},
           "provider": {"type": "string",
                        "description": "optional, e.g. route:auto"}},
          ["name", "prompt", "cron"], WRITE, _schedule),
        t("list_schedules", "List scheduled tasks and when they run next.",
          {}, [], READ, _schedules),
    ]
