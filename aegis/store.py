"""Chat persistence: SQLite, full-text search, and a per-chat image budget.

Two representations of the same conversation are stored, deliberately:

  canonical   what goes back to the model - roles, content, tool_calls,
              tool results, and the images those tools returned. This is the
              source of truth for continuing a conversation.

  display     the exact part list the UI renders - text, tool traces, approval
              outcomes, step markers. Stored so a reloaded chat looks identical
              to how it looked live, rather than a reconstruction that quietly
              loses the approvals and step boundaries.

Images live in their own table as raw bytes, referenced by id from both. That
keeps the messages table small, lets the UI fetch a screenshot lazily, and makes
the image budget enforceable without rewriting message rows.

The budget matters: one computer-use session can produce fifty screenshots. Past
the per-chat limit the oldest images are evicted and the trace shows that they
were dropped, rather than the database growing without bound.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ensure_dirs, settings

DB_PATH = DATA_DIR / "chats.db"
MB = 1024 * 1024

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_fts_available = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    parts       TEXT NOT NULL DEFAULT '[]',
    created     REAL NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_chat_seq ON messages(chat_id, seq);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id);

CREATE TABLE IF NOT EXISTS blobs (
    id          TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    media_type  TEXT NOT NULL DEFAULT 'image/png',
    bytes       INTEGER NOT NULL DEFAULT 0,
    data        BLOB NOT NULL,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_blobs_chat ON blobs(chat_id, created);
"""


def connect() -> sqlite3.Connection:
    """One connection, guarded by a lock. Fine for a single-user local app."""
    global _conn, _fts_available
    with _lock:
        if _conn is not None:
            return _conn
        ensure_dirs()
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)

        # FTS5 is compiled into most Python builds, but not all. Search falls
        # back to LIKE rather than the feature disappearing.
        try:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(content, chat_id UNINDEXED, seq UNINDEXED);
            """)
            _fts_available = True
        except sqlite3.OperationalError:
            _fts_available = False

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


def search_backend() -> str:
    connect()
    return "fts5" if _fts_available else "like"


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

def create_chat(title: str = "", provider: str = "", model: str = "") -> str:
    conn = connect()
    chat_id = uuid.uuid4().hex[:16]
    now = time.time()
    with _lock:
        conn.execute(
            "INSERT INTO chats (id, title, created, updated, provider, model) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, title.strip()[:200], now, now, provider, model))
        conn.commit()
    return chat_id


def get_chat(chat_id: str) -> dict[str, Any] | None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return None
        counts = conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(tokens_in),0) AS ti, COALESCE(SUM(tokens_out),0) AS to_ "
            "FROM messages WHERE chat_id=?", (chat_id,)).fetchone()
        images = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
            "FROM blobs WHERE chat_id=?", (chat_id,)).fetchone()
    return {
        **dict(row),
        "messages": counts["n"],
        "tokens_in": counts["ti"],
        "tokens_out": counts["to_"],
        "images": images["n"],
        "image_bytes": images["b"],
    }


def list_chats(query: str = "", archived: bool = False,
               limit: int = 200) -> list[dict[str, Any]]:
    """Pinned first, then most recently used. `query` searches message text."""
    conn = connect()
    with _lock:
        if query.strip():
            ids = _search_ids(conn, query.strip(), limit)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM chats WHERE id IN ({placeholders}) "
                f"AND archived=? ORDER BY pinned DESC, updated DESC",
                (*ids, int(archived))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chats WHERE archived=? "
                "ORDER BY pinned DESC, updated DESC LIMIT ?",
                (int(archived), limit)).fetchall()

        out = []
        for row in rows:
            preview = conn.execute(
                "SELECT content FROM messages WHERE chat_id=? AND role='user' "
                "ORDER BY seq LIMIT 1", (row["id"],)).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_id=?",
                (row["id"],)).fetchone()["n"]
            out.append({**dict(row), "messages": count,
                        "preview": (preview["content"] if preview else "")[:140]})
    return out


def _search_ids(conn: sqlite3.Connection, query: str, limit: int) -> list[str]:
    if _fts_available:
        try:
            rows = conn.execute(
                "SELECT DISTINCT chat_id FROM messages_fts WHERE messages_fts "
                "MATCH ? LIMIT ?", (_fts_query(query), limit)).fetchall()
            return [r["chat_id"] for r in rows]
        except sqlite3.OperationalError:
            pass                                   # malformed query, fall through
    rows = conn.execute(
        "SELECT DISTINCT chat_id FROM messages WHERE content LIKE ? LIMIT ?",
        (f"%{query}%", limit)).fetchall()
    return [r["chat_id"] for r in rows]


def _fts_query(query: str) -> str:
    """Quote each term so user punctuation cannot break the FTS grammar."""
    terms = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms) or '""'


def update_chat(chat_id: str, **fields: Any) -> bool:
    allowed = {"title", "pinned", "archived", "provider", "model"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    conn = connect()
    sets = ", ".join(f"{k}=?" for k in updates)
    with _lock:
        cur = conn.execute(f"UPDATE chats SET {sets} WHERE id=?",
                           (*updates.values(), chat_id))
        conn.commit()
    return cur.rowcount > 0


def touch(chat_id: str, provider: str = "", model: str = "") -> None:
    conn = connect()
    with _lock:
        if provider or model:
            conn.execute("UPDATE chats SET updated=?, provider=?, model=? WHERE id=?",
                         (time.time(), provider, model, chat_id))
        else:
            conn.execute("UPDATE chats SET updated=? WHERE id=?",
                         (time.time(), chat_id))
        conn.commit()


def delete_chat(chat_id: str) -> bool:
    conn = connect()
    with _lock:
        conn.execute("DELETE FROM blobs WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        if _fts_available:
            conn.execute("DELETE FROM messages_fts WHERE chat_id=?", (chat_id,))
        cur = conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def image_budget_bytes() -> int:
    return int(settings.get("chat_image_budget_mb", 48)) * MB


def put_image(chat_id: str, media_type: str, b64: str) -> str | None:
    """Store one image and enforce the per-chat budget. Returns a blob id."""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None

    conn = connect()
    blob_id = uuid.uuid4().hex[:20]
    with _lock:
        conn.execute(
            "INSERT INTO blobs (id, chat_id, media_type, bytes, data, created) "
            "VALUES (?,?,?,?,?,?)",
            (blob_id, chat_id, media_type, len(raw), raw, time.time()))
        conn.commit()
    _evict_images(chat_id)
    return blob_id


def _evict_images(chat_id: str) -> int:
    """Drop the oldest images until this chat is inside its budget.

    The newest image is never evicted - a run that just took a screenshot must
    still be able to show the model what it is looking at.
    """
    conn = connect()
    budget = image_budget_bytes()
    dropped = 0
    with _lock:
        while True:
            total = conn.execute(
                "SELECT COALESCE(SUM(bytes),0) AS b FROM blobs WHERE chat_id=?",
                (chat_id,)).fetchone()["b"]
            if total <= budget:
                break
            rows = conn.execute(
                "SELECT id FROM blobs WHERE chat_id=? ORDER BY created LIMIT 2",
                (chat_id,)).fetchall()
            if len(rows) < 2:
                break
            conn.execute("DELETE FROM blobs WHERE id=?", (rows[0]["id"],))
            dropped += 1
        if dropped:
            conn.commit()
    return dropped


def get_image(blob_id: str) -> tuple[str, bytes] | None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT media_type, data FROM blobs WHERE id=?",
                           (blob_id,)).fetchone()
    return (row["media_type"], row["data"]) if row else None


def image_b64(blob_id: str) -> tuple[str, str] | None:
    found = get_image(blob_id)
    if not found:
        return None
    media_type, raw = found
    return media_type, base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _next_seq(conn: sqlite3.Connection, chat_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), -1) AS s FROM messages "
                       "WHERE chat_id=?", (chat_id,)).fetchone()
    return int(row["s"]) + 1


def add_message(chat_id: str, role: str, content: str = "", *,
                meta: dict[str, Any] | None = None,
                parts: list[dict[str, Any]] | None = None,
                images: list[tuple[str, str]] | None = None,
                tokens_in: int = 0, tokens_out: int = 0) -> int:
    """Append one canonical message. Images are stored and referenced by id."""
    conn = connect()
    meta = dict(meta or {})

    if images:
        ids = [bid for media_type, b64 in images
               if (bid := put_image(chat_id, media_type, b64))]
        meta["image_ids"] = ids

    with _lock:
        seq = _next_seq(conn, chat_id)
        conn.execute(
            "INSERT INTO messages (chat_id, seq, role, content, meta, parts, "
            "created, tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, seq, role, content, json.dumps(meta),
             json.dumps(parts or []), time.time(), tokens_in, tokens_out))
        if _fts_available and content:
            conn.execute("INSERT INTO messages_fts (content, chat_id, seq) "
                         "VALUES (?,?,?)", (content, chat_id, seq))
        conn.execute("UPDATE chats SET updated=? WHERE id=?",
                     (time.time(), chat_id))
        conn.commit()

    if role == "user":
        _maybe_title(chat_id, content)
    return seq


def update_message(chat_id: str, seq: int, *, content: str | None = None,
                   meta: dict[str, Any] | None = None,
                   parts: list[dict[str, Any]] | None = None,
                   tokens_in: int | None = None,
                   tokens_out: int | None = None) -> None:
    """Rewrite a message in place - used to checkpoint a streaming reply."""
    conn = connect()
    sets: list[str] = []
    values: list[Any] = []
    if content is not None:
        sets.append("content=?"); values.append(content)
    if meta is not None:
        sets.append("meta=?"); values.append(json.dumps(meta))
    if parts is not None:
        sets.append("parts=?"); values.append(json.dumps(parts))
    if tokens_in is not None:
        sets.append("tokens_in=?"); values.append(tokens_in)
    if tokens_out is not None:
        sets.append("tokens_out=?"); values.append(tokens_out)
    if not sets:
        return
    with _lock:
        conn.execute(f"UPDATE messages SET {', '.join(sets)} "
                     f"WHERE chat_id=? AND seq=?", (*values, chat_id, seq))
        if content is not None and _fts_available:
            conn.execute("DELETE FROM messages_fts WHERE chat_id=? AND seq=?",
                         (chat_id, seq))
            if content:
                conn.execute("INSERT INTO messages_fts (content, chat_id, seq) "
                             "VALUES (?,?,?)", (content, chat_id, seq))
        conn.commit()


def raw_messages(chat_id: str) -> list[dict[str, Any]]:
    conn = connect()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY seq",
            (chat_id,)).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        entry["meta"] = json.loads(entry["meta"] or "{}")
        entry["parts"] = json.loads(entry["parts"] or "[]")
        out.append(entry)
    return out


def _maybe_title(chat_id: str, text: str) -> None:
    """Name a chat after its first user message, once."""
    conn = connect()
    with _lock:
        row = conn.execute("SELECT title FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not row or (row["title"] or "").strip():
            return
    title = " ".join((text or "").split())[:70]
    if title:
        update_chat(chat_id, title=title)


# ---------------------------------------------------------------------------
# Rebuilding the two views
# ---------------------------------------------------------------------------

def transcript_for_model(chat_id: str) -> list[dict[str, Any]]:
    """Canonical transcript, with images rehydrated from the blob table.

    An image whose blob was evicted by the budget becomes a short note, so the
    model is told the picture is gone rather than silently seeing nothing.
    """
    out: list[dict[str, Any]] = []
    for row in raw_messages(chat_id):
        role = row["role"]
        meta = row["meta"]
        message: dict[str, Any] = {"role": role, "content": row["content"]}

        if role == "assistant" and meta.get("tool_calls"):
            message["tool_calls"] = meta["tool_calls"]
        if role == "tool":
            message["tool_call_id"] = meta.get("tool_call_id", "")
            message["name"] = meta.get("name", "")
            if meta.get("is_error"):
                message["is_error"] = True

        images: list[tuple[str, str]] = []
        missing = 0
        for blob_id in meta.get("image_ids") or []:
            found = image_b64(blob_id)
            if found:
                images.append(found)
            else:
                missing += 1
        if images:
            message["images"] = images
        if missing:
            message["content"] = (
                f"{message['content']}\n\n[{missing} earlier image(s) were "
                f"dropped to stay inside this chat's image budget.]").strip()

        out.append(message)
    return out


def transcript_for_display(chat_id: str) -> list[dict[str, Any]]:
    """Exactly what the UI renders: user turns, and assistant turns with parts.

    One agent turn produces several canonical assistant rows (one per model
    turn) plus tool rows, but the UI shows it as a single reply whose trace
    holds everything. The first row of a turn carries meta.display=True and the
    whole part list; the rest are marked False and skipped here.
    """
    out: list[dict[str, Any]] = []
    for row in raw_messages(chat_id):
        if row["role"] == "user":
            out.append({"role": "user", "content": row["content"]})
        elif row["role"] == "assistant" and row["meta"].get("display", True):
            out.append({
                "role": "assistant",
                "content": row["content"],
                "parts": row["parts"] or ([{"kind": "text",
                                            "text": row["content"]}]
                                          if row["content"] else []),
                "usage": ({"input": row["tokens_in"], "output": row["tokens_out"]}
                          if row["tokens_in"] or row["tokens_out"] else None),
                "error": row["meta"].get("error", ""),
            })
        # 'tool' rows are already represented inside the assistant turn's parts
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(chat_id: str) -> dict[str, Any]:
    chat = get_chat(chat_id)
    if not chat:
        return {}
    return {"chat": chat, "messages": raw_messages(chat_id),
            "exported": time.time(), "format": "aegis-chat-v1"}


def export_markdown(chat_id: str) -> str:
    chat = get_chat(chat_id)
    if not chat:
        return ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(chat["created"]))
    lines = [f"# {chat['title'] or 'Untitled chat'}", "",
             f"*{when} · {chat['provider']} · {chat['model']}*", ""]

    for row in raw_messages(chat_id):
        role, meta = row["role"], row["meta"]
        if role == "user":
            lines += ["## You", "", row["content"], ""]
        elif role == "assistant":
            lines += ["## Assistant", ""]
            for part in row["parts"] or []:
                kind = part.get("kind")
                if kind == "text":
                    lines += [part.get("text", ""), ""]
                elif kind == "step":
                    lines += [f"*— step {part.get('n')} —*", ""]
                elif kind == "tool":
                    mark = "✓" if part.get("ok") else "✗"
                    lines += [f"> **{mark} {part.get('name', '')}** "
                              f"`{part.get('argsText', '')}`", ">"]
                    for line in (str(part.get("result", "")).splitlines() or [""]):
                        lines.append(f"> {line}")
                    lines.append("")
                elif kind == "approval":
                    state = ("approved" if part.get("approved")
                             else "declined" if part.get("resolved") else "pending")
                    lines += [f"> *approval for `{part.get('tool')}`: {state}*", ""]
            if not row["parts"] and row["content"]:
                lines += [row["content"], ""]
            if meta.get("error"):
                lines += [f"> **Error:** {meta['error']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def stats() -> dict[str, Any]:
    conn = connect()
    with _lock:
        chats = conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
        msgs = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        blob = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
                            "FROM blobs").fetchone()
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"chats": chats, "messages": msgs, "images": blob["n"],
            "image_bytes": blob["b"], "db_bytes": size,
            "db_path": str(DB_PATH), "search": search_backend(),
            "image_budget_mb": image_budget_bytes() // MB}
