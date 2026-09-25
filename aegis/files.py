"""Attachments: extract once, inline when small, search when large.

The trap this avoids: pasting a 300-page PDF into the prompt. That is 400,000
tokens, it blows past most context windows, and on a free daily allowance it
spends the whole day's budget on one message.

So every attachment is extracted to text once, on arrival, and then:

    small   the text goes straight into the message, because a two-page letter
            is cheaper to read than to search.
    large   the text is stored and indexed, and the agent pulls the parts it
            needs with files__search and files__read_attachment.

Images skip all of that and go to the model as images, if the model can see.

Extraction is best-effort and honest about it: a scanned PDF with no text layer
reports that it has no extractable text rather than silently attaching nothing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ensure_dirs

ATTACH_DIR = DATA_DIR / "attachments"
DB_PATH = DATA_DIR / "attachments.db"

# Under this many characters, the text rides along in the message itself.
INLINE_LIMIT = 12_000
GB = 1024 ** 3
# v2.1: uploads up to 30 GB by default (Settings -> max_upload_gb). Files are
# streamed straight to disk, never held in memory.
DEFAULT_MAX_UPLOAD_GB = 30
# Above this, a file is stored and handed to the tools by path, but not read
# into text - nobody wants a 12 GB log pasted into a prompt.
EXTRACT_LIMIT = 256 * 1024 * 1024
IMAGE_INLINE_LIMIT = 20 * 1024 * 1024
DISK_MARGIN = 1 * GB


def max_upload() -> int:
    from .config import settings
    try:
        gb = float(settings.get("max_upload_gb", DEFAULT_MAX_UPLOAD_GB))
    except (TypeError, ValueError):
        gb = DEFAULT_MAX_UPLOAD_GB
    if gb <= 0:
        gb = DEFAULT_MAX_UPLOAD_GB
    return int(gb * GB)


MAX_UPLOAD = DEFAULT_MAX_UPLOAD_GB * GB
PREVIEW_CHARS = 600

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".conf", ".toml",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".psm1", ".sql",
    ".css", ".scss", ".vue", ".svelte", ".r", ".jsonl", ".env", ".gitignore",
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_fts = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id        TEXT PRIMARY KEY,
    chat_id   TEXT NOT NULL DEFAULT '',
    name      TEXT NOT NULL,
    path      TEXT NOT NULL,
    media     TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'text',
    bytes     INTEGER NOT NULL DEFAULT 0,
    chars     INTEGER NOT NULL DEFAULT 0,
    pages     INTEGER NOT NULL DEFAULT 0,
    inline    INTEGER NOT NULL DEFAULT 0,
    note      TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attach_chat ON attachments(chat_id);
"""


def connect() -> sqlite3.Connection:
    global _conn, _fts
    with _lock:
        if _conn is not None:
            return _conn
        ensure_dirs()
        ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            conn.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS attach_fts "
                "USING fts5(text, attach_id UNINDEXED, name, chat_id UNINDEXED);")
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


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@dataclass
class Extracted:
    text: str = ""
    kind: str = "text"        # text | pdf | docx | xlsx | image | binary
    pages: int = 0
    note: str = ""


def _decode(data: bytes) -> str:
    """Decode text, only trying UTF-16 when a BOM actually says so.

    Trying UTF-16 blind is a trap: any even-length byte string "decodes"
    successfully into nonsense, so b'caf\\xe9' (cp1252 for "café") comes back as
    '慣\\ue966' rather than falling through to the encoding that was right.
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _extract_pdf(data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
    except ImportError:
        return Extracted(kind="pdf", note="pypdf is not installed, so the text "
                                          "could not be extracted.")
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        return Extracted(kind="pdf", note=f"Could not read the PDF: {exc}")

    text = "\n\n".join(f"--- page {i + 1} ---\n{t.strip()}"
                       for i, t in enumerate(pages) if t.strip())
    if not text.strip():
        return Extracted(kind="pdf", pages=len(pages),
                         note=(f"{len(pages)} page(s), but no text layer - this "
                               f"looks like a scan. It needs OCR, which AEGIS "
                               f"does not do."))
    return Extracted(text=text, kind="pdf", pages=len(pages))


def _extract_docx(data: bytes) -> Extracted:
    try:
        import docx
    except ImportError:
        return Extracted(kind="docx", note="python-docx is not installed.")
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        return Extracted(kind="docx", note=f"Could not read the document: {exc}")

    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return Extracted(text="\n".join(parts), kind="docx")


def _extract_xlsx(data: bytes) -> Extracted:
    try:
        import openpyxl
    except ImportError:
        return Extracted(kind="xlsx", note="openpyxl is not installed.")
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                      data_only=True)
    except Exception as exc:
        return Extracted(kind="xlsx", note=f"Could not read the workbook: {exc}")

    chunks: list[str] = []
    for sheet in book.worksheets:
        chunks.append(f"--- sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            values = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in values):
                chunks.append(" | ".join(values))
    book.close()
    return Extracted(text="\n".join(chunks), kind="xlsx")


def _extract_csv(data: bytes) -> Extracted:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4000])
        rows = list(csv.reader(io.StringIO(text), dialect))
    except Exception:
        rows = list(csv.reader(io.StringIO(text)))
    lines = [" | ".join(str(c) for c in row) for row in rows if any(row)]
    header = f"{len(rows)} rows, {len(rows[0]) if rows else 0} columns\n" if rows else ""
    return Extracted(text=header + "\n".join(lines), kind="text")


def extract(name: str, data: bytes) -> Extracted:
    """Turn any supported file into text. Never raises."""
    suffix = Path(name).suffix.lower()

    if suffix in IMAGE_TYPES:
        return Extracted(kind="image", note="Sent to the model as an image.")
    if suffix == ".pdf":
        return _extract_pdf(data)
    if suffix in (".docx", ".dotx"):
        return _extract_docx(data)
    if suffix in (".xlsx", ".xlsm", ".xltx"):
        return _extract_xlsx(data)
    if suffix in (".csv", ".tsv"):
        return _extract_csv(data)
    if suffix == ".json":
        try:
            return Extracted(text=json.dumps(json.loads(_decode(data)), indent=2),
                             kind="text")
        except ValueError:
            return Extracted(text=_decode(data), kind="text")
    if suffix in TEXT_SUFFIXES or not suffix:
        return Extracted(text=_decode(data), kind="text")

    # Unknown extension: if it decodes cleanly it is probably text.
    sample = data[:4096]
    if b"\x00" not in sample:
        return Extracted(text=_decode(data), kind="text")
    return Extracted(kind="binary",
                     note=f"{suffix or 'This file'} is binary; AEGIS cannot read it.")


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w\-. ()\[\]]", "_", name).strip() or "file"
    return name[:120]


def human(n: int) -> str:
    for unit, size in (("GB", GB), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def new_path(chat_id: str, name: str) -> Path:
    """Where a streamed upload is written."""
    folder = ATTACH_DIR / (chat_id or "loose")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{uuid.uuid4().hex[:8]}-{_safe_name(name)}"


def check_space(size: int) -> str:
    """'' if a file of this size can be accepted, otherwise why not."""
    import shutil
    limit = max_upload()
    if size > limit:
        return (f"That file is {human(size)}; the upload limit is {human(limit)} "
                f"(Settings -> max_upload_gb).")
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(ATTACH_DIR).free
    if size and free - size < DISK_MARGIN:
        return (f"Not enough disk space: {human(size)} needed, {human(free)} free "
                f"on the drive that holds AEGIS's data.")
    return ""


def add(chat_id: str, name: str, data: bytes) -> dict[str, Any]:
    """Store one attachment from memory (small uploads, tests)."""
    if len(data) > max_upload():
        return {"ok": False,
                "error": f"{_safe_name(name)} is {human(len(data))}; the limit is "
                         f"{human(max_upload())}."}
    path = new_path(chat_id, name)
    path.write_bytes(data)
    return add_path(chat_id, name, path)


def add_path(chat_id: str, name: str, path: Path) -> dict[str, Any]:
    """Register a file that is already on disk (a streamed upload).

    Small files are read and extracted as before. Big ones - videos, disk
    images, support bundles, datasets - are stored and passed to the tools by
    path, so a 30 GB file costs nothing in memory or tokens.
    """
    conn = connect()
    name = _safe_name(name)
    size = path.stat().st_size
    suffix = Path(name).suffix.lower()

    if suffix in IMAGE_TYPES and size > IMAGE_INLINE_LIMIT:
        result = Extracted(kind="large", note=f"Large image ({human(size)}) - "
                                              f"stored; tools can open it by path.")
    elif size > EXTRACT_LIMIT:
        result = Extracted(kind="large", note=f"{human(size)} - stored on disk; too "
                                              f"big to read into a prompt, so the "
                                              f"tools work on it by path.")
    else:
        result = extract(name, path.read_bytes())

    attach_id = uuid.uuid4().hex[:16]
    media = (IMAGE_TYPES.get(suffix)
             or mimetypes.guess_type(name)[0] or "application/octet-stream")
    inline = bool(result.text) and len(result.text) <= INLINE_LIMIT

    with _lock:
        conn.execute(
            "INSERT INTO attachments (id, chat_id, name, path, media, kind, "
            "bytes, chars, pages, inline, note, text, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attach_id, chat_id, name, str(path), media, result.kind,
             size, len(result.text), result.pages, int(inline),
             result.note, result.text, time.time()))
        if _fts and result.text:
            conn.execute("INSERT INTO attach_fts (text, attach_id, name, chat_id) "
                         "VALUES (?,?,?,?)", (result.text, attach_id, name, chat_id))
        conn.commit()

    return {"ok": True, **record(attach_id)}


def path_of(attach_id: str) -> Path | None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT path FROM attachments WHERE id=?",
                           (attach_id,)).fetchone()
    return Path(row["path"]) if row else None


def place_in_workspace(src: Path, dest_dir: Path) -> Path | None:
    """Put an upload into the workspace without doubling a huge file on disk:
    a hard link when possible (same drive, instant, no extra space), a copy
    for smaller files, otherwise the tools use the stored path directly."""
    import os
    import shutil
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / re.sub(r"^[0-9a-f]{8}-", "", src.name)
        if dest.exists():
            dest = dest_dir / f"{dest.stem}-{uuid.uuid4().hex[:4]}{dest.suffix}"
        try:
            os.link(src, dest)
            return dest
        except OSError:
            if src.stat().st_size <= 1 * GB:
                shutil.copyfile(src, dest)
                return dest
    except OSError:
        pass
    return None


def record(attach_id: str) -> dict[str, Any]:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT * FROM attachments WHERE id=?",
                           (attach_id,)).fetchone()
    if not row:
        return {}
    data = dict(row)
    data.pop("text", None)
    data["inline"] = bool(data["inline"])
    data["preview"] = (row["text"] or "")[:PREVIEW_CHARS]
    return data


def for_chat(chat_id: str) -> list[dict[str, Any]]:
    conn = connect()
    with _lock:
        rows = conn.execute("SELECT id FROM attachments WHERE chat_id=? "
                            "ORDER BY created", (chat_id,)).fetchall()
    return [record(r["id"]) for r in rows]


def text_of(attach_id: str, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT name, text, chars, note FROM attachments "
                           "WHERE id=?", (attach_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"No attachment {attach_id!r}."}
    text = row["text"] or ""
    if not text:
        return {"ok": True, "name": row["name"], "text": "",
                "note": row["note"] or "This file has no extractable text."}
    chunk = text[offset:offset + limit]
    return {"ok": True, "name": row["name"], "text": chunk,
            "offset": offset, "returned": len(chunk), "total": len(text),
            "more": offset + len(chunk) < len(text)}


def bytes_of(attach_id: str) -> tuple[str, bytes] | None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT media, path FROM attachments WHERE id=?",
                           (attach_id,)).fetchone()
    if not row:
        return None
    try:
        return row["media"], Path(row["path"]).read_bytes()
    except OSError:
        return None


def search(query: str, chat_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    query = (query or "").strip()
    if not query:
        return []
    with _lock:
        if _fts:
            terms = " ".join(f'"{t}"' for t in query.replace('"', " ").split())
            try:
                sql = ("SELECT attach_id, name, snippet(attach_fts, 0, '<<', '>>', "
                       "'...', 20) AS snip FROM attach_fts WHERE attach_fts MATCH ?")
                params: list[Any] = [terms or '""']
                if chat_id:
                    sql += " AND chat_id = ?"
                    params.append(chat_id)
                rows = conn.execute(sql + " LIMIT ?", (*params, limit)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        sql = "SELECT id AS attach_id, name, substr(text,1,300) AS snip FROM attachments WHERE text LIKE ?"
        params = [f"%{query}%"]
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        rows = conn.execute(sql + " LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def delete(attach_id: str) -> bool:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT path FROM attachments WHERE id=?",
                           (attach_id,)).fetchone()
        if not row:
            return False
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        conn.execute("DELETE FROM attachments WHERE id=?", (attach_id,))
        if _fts:
            conn.execute("DELETE FROM attach_fts WHERE attach_id=?", (attach_id,))
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# What goes into the message
# ---------------------------------------------------------------------------

def message_parts(attachment_ids: list[str], copy_to: Path | None = None
                  ) -> tuple[str, list[tuple[str, str]]]:
    """Build the text block and image list for a user message.

    Small text rides inline. Large text is announced, not pasted - the agent is
    told the id and how to search it.
    """
    import base64

    blocks: list[str] = []
    images: list[tuple[str, str]] = []

    for attach_id in attachment_ids:
        meta = record(attach_id)
        if not meta:
            continue

        # v2.1: every upload - any type, zip, audio, video - also lands in the
        # workspace, so the code, media and voice tools can work on it.
        stored = path_of(attach_id)
        if copy_to is not None and stored is not None and stored.is_file():
            dest = place_in_workspace(stored, copy_to)
            where = dest or stored
            blocks.append(f"[file saved to the workspace: {where}]")

        if meta["kind"] == "large":
            blocks.append(f"[attached: {meta['name']} - {meta['note']} Path: "
                          f"{stored}. Use code__run / code__python (or "
                          f"voice__transcribe for audio/video) on that path; read "
                          f"it in pieces rather than all at once.]")
            continue

        if meta["kind"] == "image":
            found = bytes_of(attach_id)
            if found:
                media, raw = found
                images.append((media, base64.b64encode(raw).decode()))
                blocks.append(f"[attached image: {meta['name']}]")
            continue

        if meta["inline"]:
            body = text_of(attach_id, 0, INLINE_LIMIT + 1000)
            blocks.append(f"--- attached file: {meta['name']} ---\n"
                          f"{body.get('text', '')}\n--- end of {meta['name']} ---")
        elif meta["chars"]:
            size = f"{meta['chars']:,} characters"
            if meta["pages"]:
                size += f", {meta['pages']} pages"
            blocks.append(
                f"[attached: {meta['name']} - {size}. Too large to include in "
                f"full. Its id is {attach_id}; use files__search_attachments to "
                f"find the relevant parts, then files__read_attachment to read "
                f"them.]")
        else:
            blocks.append(f"[attached: {meta['name']} - {meta['note'] or 'no text'}]")

    return "\n\n".join(blocks), images


def stats() -> dict[str, Any]:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
                           "FROM attachments").fetchone()
    return {"attachments": row["n"], "bytes": row["b"],
            "inline_limit": INLINE_LIMIT,
            "search": "fts5" if _fts else "like"}
