"""Second Brain: an agent memory store, and a read-only index of your vault.

Two separate things that both answer "what do we already know":

  facts       durable notes the agent writes and reads across conversations.
              Small, explicit, and yours to edit or delete.

  vault       a full-text index of your Obsidian vault. Built by walking the
              folder and reading the markdown. **Nothing here writes to your
              vault** - there is no function that does, not a disabled one, not
              one behind a flag. The index is a copy; your notes are the
              original and stay untouched.

Both live in memory.db, separate from chats.db, so you can delete one without
losing the other.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ensure_dirs, settings

DB_PATH = DATA_DIR / "memory.db"
MAX_NOTE_BYTES = 1_000_000          # skip anything larger; it is not a note
VAULT_SUFFIXES = {".md", ".markdown", ".txt"}
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".venv",
             "__pycache__", ".smart-env", ".space"}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_fts = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    tags    TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_docs (
    path    TEXT PRIMARY KEY,
    title   TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    mtime   REAL NOT NULL DEFAULT 0,
    bytes   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    global _conn, _fts
    with _lock:
        if _conn is not None:
            return _conn
        ensure_dirs()
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            conn.executescript("""
              CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(text, fact_id UNINDEXED);
              CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts
                USING fts5(content, path UNINDEXED, title);
            """)
            _fts = True
        except sqlite3.OperationalError:
            _fts = False
        conn.commit()
        _conn = conn
        return conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.commit()
            _conn.close()
            _conn = None


def _fts_query(query: str) -> str:
    terms = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms) or '""'


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def save_fact(text: str, tags: str = "", source: str = "agent") -> int:
    text = (text or "").strip()
    if not text:
        return 0
    conn = connect()
    now = time.time()
    with _lock:
        cur = conn.execute(
            "INSERT INTO facts (text, tags, source, created, updated) "
            "VALUES (?,?,?,?,?)", (text[:8000], tags.strip()[:200], source, now, now))
        fact_id = int(cur.lastrowid)
        if _fts:
            conn.execute("INSERT INTO facts_fts (text, fact_id) VALUES (?,?)",
                         (text, fact_id))
        conn.commit()
    return fact_id


def search_facts(query: str = "", tags: str = "", limit: int = 20) -> list[dict[str, Any]]:
    conn = connect()
    with _lock:
        if query.strip() and _fts:
            try:
                rows = conn.execute(
                    "SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.fact_id "
                    "WHERE facts_fts MATCH ? ORDER BY f.updated DESC LIMIT ?",
                    (_fts_query(query), limit)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        clauses, values = [], []
        if query.strip():
            clauses.append("text LIKE ?")
            values.append(f"%{query.strip()}%")
        if tags.strip():
            clauses.append("tags LIKE ?")
            values.append(f"%{tags.strip()}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM facts {where} ORDER BY updated DESC LIMIT ?",
            (*values, limit)).fetchall()
    return [dict(r) for r in rows]


def forget_fact(fact_id: int) -> bool:
    conn = connect()
    with _lock:
        cur = conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        if _fts:
            conn.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact_id,))
        conn.commit()
    return cur.rowcount > 0


def count_facts() -> int:
    conn = connect()
    with _lock:
        return int(conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"])


# ---------------------------------------------------------------------------
# Vault index (read-only)
# ---------------------------------------------------------------------------

def vault_path() -> Path | None:
    raw = (settings.get("vault_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def _title_of(text: str, path: Path) -> str:
    for line in text.splitlines()[:20]:
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return path.stem


def reindex_vault(force: bool = False) -> dict[str, Any]:
    """Walk the vault and refresh the index. Reads only; never writes there."""
    root = vault_path()
    if not root:
        return {"ok": False, "error": "No vault folder set. Point AEGIS at your "
                                      "Obsidian folder in Settings."}
    conn = connect()
    started = time.time()
    seen: set[str] = set()
    added = updated = skipped = 0

    known: dict[str, float] = {}
    with _lock:
        for row in conn.execute("SELECT path, mtime FROM vault_docs").fetchall():
            known[row["path"]] = row["mtime"]

    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in VAULT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in file.parts):
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        if stat.st_size > MAX_NOTE_BYTES:
            skipped += 1
            continue

        rel = str(file.relative_to(root)).replace("\\", "/")
        seen.add(rel)
        if not force and known.get(rel, 0) >= stat.st_mtime:
            continue
        try:
            text = file.read_text("utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        title = _title_of(text, file)
        with _lock:
            existed = rel in known
            conn.execute(
                "INSERT INTO vault_docs (path, title, content, mtime, bytes) "
                "VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "title=excluded.title, content=excluded.content, "
                "mtime=excluded.mtime, bytes=excluded.bytes",
                (rel, title, text, stat.st_mtime, stat.st_size))
            if _fts:
                conn.execute("DELETE FROM vault_fts WHERE path=?", (rel,))
                conn.execute("INSERT INTO vault_fts (content, path, title) "
                             "VALUES (?,?,?)", (text, rel, title))
        updated += existed
        added += not existed

    removed = 0
    with _lock:
        for rel in list(known):
            if rel not in seen:
                conn.execute("DELETE FROM vault_docs WHERE path=?", (rel,))
                if _fts:
                    conn.execute("DELETE FROM vault_fts WHERE path=?", (rel,))
                removed += 1
        conn.execute("INSERT INTO meta (k, v) VALUES ('vault_indexed', ?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (str(time.time()),))
        conn.commit()

    return {"ok": True, "added": added, "updated": updated, "removed": removed,
            "skipped": skipped, "total": len(seen),
            "seconds": round(time.time() - started, 2), "root": str(root)}


def search_vault(query: str, limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    query = (query or "").strip()
    if not query:
        return []
    with _lock:
        if _fts:
            try:
                rows = conn.execute(
                    "SELECT v.path, v.title, snippet(vault_fts, 0, '<<', '>>', '…', 24) "
                    "AS snip FROM vault_fts v WHERE vault_fts MATCH ? LIMIT ?",
                    (_fts_query(query), limit)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            "SELECT path, title, substr(content, 1, 300) AS snip FROM vault_docs "
            "WHERE content LIKE ? LIMIT ?", (f"%{query}%", limit)).fetchall()
    return [dict(r) for r in rows]


def read_vault_note(path: str, max_chars: int = 20000) -> dict[str, Any]:
    conn = connect()
    path = (path or "").strip().replace("\\", "/")
    with _lock:
        row = conn.execute(
            "SELECT path, title, content, bytes FROM vault_docs WHERE path=?",
            (path,)).fetchone()
        if not row:
            like = conn.execute(
                "SELECT path, title, content, bytes FROM vault_docs "
                "WHERE path LIKE ? LIMIT 1", (f"%{path}%",)).fetchone()
            row = like
    if not row:
        return {"ok": False, "error": f"No note indexed at {path!r}. "
                                      f"Use vault_search to find the right path."}
    content = row["content"]
    truncated = len(content) > max_chars
    return {"ok": True, "path": row["path"], "title": row["title"],
            "content": content[:max_chars], "truncated": truncated,
            "bytes": row["bytes"]}


def vault_stats() -> dict[str, Any]:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
                           "FROM vault_docs").fetchone()
        indexed = conn.execute("SELECT v FROM meta WHERE k='vault_indexed'").fetchone()
    root = vault_path()
    return {"configured": bool(settings.get("vault_path")),
            "path": str(root) if root else (settings.get("vault_path") or ""),
            "exists": root is not None,
            "notes": row["n"], "bytes": row["b"],
            "indexed_at": float(indexed["v"]) if indexed else 0.0,
            "facts": count_facts(),
            "search": "fts5" if _fts else "like",
            "writes_to_vault": False}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tools() -> list[Any]:
    import asyncio

    from .tools.base import READ, WRITE, Tool

    def wrap(fn, **fixed):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await asyncio.to_thread(fn, **{**fixed, **args})
            except Exception as exc:
                return {"text": f"{type(exc).__name__}: {exc}", "error": True}
        return handler

    def _save(text: str = "", tags: str = "") -> dict[str, Any]:
        fact_id = save_fact(text, tags)
        if not fact_id:
            return {"text": "Nothing to save.", "error": True}
        return {"text": f"Saved as memory #{fact_id}."}

    def _search(query: str = "", tags: str = "", limit: int = 20) -> dict[str, Any]:
        rows = search_facts(query, tags, min(int(limit or 20), 50))
        if not rows:
            return {"text": "Nothing in memory matches that."}
        lines = [f"#{r['id']} [{r['tags'] or 'untagged'}] {r['text']}" for r in rows]
        return {"text": "\n".join(lines)}

    def _forget(id: int = 0) -> dict[str, Any]:
        gone = forget_fact(int(id))          # call once - it is destructive
        return {"text": f"Forgot memory #{id}." if gone else f"No memory #{id}.",
                "error": not gone}

    def _vsearch(query: str = "", limit: int = 10) -> dict[str, Any]:
        rows = search_vault(query, min(int(limit or 10), 25))
        if not rows:
            stats = vault_stats()
            if not stats["notes"]:
                return {"text": "The vault index is empty. Set the vault folder "
                                "in Settings and reindex."}
            return {"text": "No notes matched that."}
        lines = [f"{r['path']} — {r['title']}\n    {r['snip']}" for r in rows]
        return {"text": "\n".join(lines)}

    def _vread(path: str = "") -> dict[str, Any]:
        result = read_vault_note(path)
        if not result.get("ok"):
            return {"text": result.get("error", "Not found."), "error": True}
        suffix = "\n\n[truncated]" if result["truncated"] else ""
        return {"text": f"# {result['title']}\n({result['path']})\n\n"
                        f"{result['content']}{suffix}"}

    def _vreindex(force: bool = False) -> dict[str, Any]:
        result = reindex_vault(bool(force))
        if not result.get("ok"):
            return {"text": result["error"], "error": True}
        return {"text": f"Indexed {result['total']} notes "
                        f"({result['added']} new, {result['updated']} changed, "
                        f"{result['removed']} gone) in {result['seconds']}s."}

    def tool(name, desc, schema, risk, handler) -> Tool:
        return Tool(name=f"memory__{name}", raw_name=name, description=desc,
                    input_schema=schema, server="memory", origin="builtin",
                    risk=risk, handler=handler)

    return [
        tool("save",
             "Save a durable fact worth remembering across conversations. Use it "
             "for stable things - decisions, preferences, how something is set "
             "up - not for passing detail.",
             {"type": "object",
              "properties": {"text": {"type": "string"},
                             "tags": {"type": "string",
                                      "description": "Comma-separated, optional"}},
              "required": ["text"]},
             WRITE, wrap(_save)),

        tool("search", "Search everything saved to memory.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "tags": {"type": "string"},
                             "limit": {"type": "integer"}}},
             READ, wrap(_search)),

        tool("forget", "Delete one saved memory by its id.",
             {"type": "object", "properties": {"id": {"type": "integer"}},
              "required": ["id"]},
             WRITE, wrap(_forget)),

        tool("vault_search",
             "Full-text search the Obsidian vault. Returns paths and snippets - "
             "follow up with vault_read for the note itself.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": ["query"]},
             READ, wrap(_vsearch)),

        tool("vault_read", "Read one note from the vault by its path.",
             {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"]},
             READ, wrap(_vread)),

        tool("vault_reindex",
             "Rebuild the vault index after notes have changed. Reads the vault; "
             "never writes to it.",
             {"type": "object",
              "properties": {"force": {"type": "boolean",
                                       "description": "Re-read every file"}}},
             READ, wrap(_vreindex)),
    ]
