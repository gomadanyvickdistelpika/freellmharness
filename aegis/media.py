"""Studio: images, speech, music and video - routed like the chat models are.

Each kind of media has an ordered list of *backends*. Generating walks that list
exactly the way a chat route does: a backend that is out of credit, rate
limited, down or simply refuses is rested and the next one is tried, so asking
for a picture gives you a picture rather than an error. Free and paid never mix
unless you say so (Settings -> "Studio may use paid backends").

A backend is a method plus where to send it:

    pollinations_image   GET  {root}/image/{prompt}?model=&width=&height=&seed=
    openai_image         POST {base}/images/generations        (OpenAI shape)
    chat_image           POST {base}/chat/completions with modalities=[image]
                                                               (OpenRouter shape)
    openai_speech        POST {base}/audio/speech              (OpenAI shape)
    openai_video         POST {base}/video/generations, bytes / url / polled job
    http_post            POST {base}{path} with a JSON template - the escape
                         hatch for anything else (a music model on some gateway)
    gradio               a Hugging Face Space through gradio_client (optional)
    local_tts            Windows' own voices through pyttsx3 - never runs out
    slideshow            video fallback: images from the image chain, stitched
                         with a slow zoom (and narration) by ffmpeg
    suno_pack            music fallback: a Suno-ready style prompt + lyrics,
                         written by your chat route, saved as a file to paste

`provider` names one of your custom providers, so a backend reuses its base URL
and API key from the vault - add Pollinations once and both its chat models and
its image endpoint are available. Whether a backend costs money is decided the
same way as for chat (router.is_paid), unless the backend says `paid` itself.

Everything generated is saved under %LOCALAPPDATA%\\Aegis\\media, indexed, shown
in the Studio gallery and in the chat, and copied into the active project's
workspace when there is one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import DATA_DIR, settings

MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "media.db"
KINDS = ("image", "speech", "music", "video")
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
       "image/gif": ".gif", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
       "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg",
       "audio/flac": ".flac", "video/mp4": ".mp4", "video/webm": ".webm",
       "text/markdown": ".md", "text/plain": ".txt"}
TIMEOUT = {"image": 120.0, "speech": 90.0, "music": 300.0, "video": 600.0}
MARKER = "[[media:{id}:{mime}]]"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


# ---------------------------------------------------------------------------
# Default backends. Provider-bound ones are skipped when you have not added
# that provider; the fallbacks at the end of each list always exist.
# ---------------------------------------------------------------------------

DEFAULT_BACKENDS: list[dict[str, Any]] = [
    {"id": "pollinations-image", "kind": "image", "label": "Pollinations",
     "method": "pollinations_image", "provider": "pollinations", "model": "",
     "enabled": True},
    {"id": "openrouter-image", "kind": "image",
     "label": "OpenRouter image model (set a :free one if listed)",
     "method": "chat_image", "provider": "openrouter",
     "model": "google/gemini-2.5-flash-image", "enabled": True},
    {"id": "xkiro-image", "kind": "image", "label": "xKiro images endpoint",
     "method": "openai_image", "provider": "xkiro", "model": "", "enabled": False},
    {"id": "pollinations-open", "kind": "image",
     "label": "Pollinations without a key (slow, limited)",
     "method": "pollinations_image", "provider": "", "model": "",
     "base_url": "https://gen.pollinations.ai", "paid": False, "enabled": True},

    {"id": "pollinations-speech", "kind": "speech", "label": "Pollinations voice",
     "method": "openai_speech", "provider": "pollinations", "model": "",
     "voice": "nova", "enabled": True},
    {"id": "local-voice", "kind": "speech", "label": "Windows voices (offline)",
     "method": "local_tts", "provider": "", "paid": False, "enabled": True},

    {"id": "space-music", "kind": "music", "label": "Hugging Face Space (set it up)",
     "method": "gradio", "provider": "", "space": "", "api_name": "",
     "args": ["{prompt}", "{lyrics}", "{duration}"], "paid": False,
     "enabled": False},
    {"id": "gateway-music", "kind": "music",
     "label": "Music model on a gateway (set path + model)",
     "method": "http_post", "provider": "pollinations", "path": "/audio/speech",
     "model": "", "body": {"model": "{model}", "input": "{prompt}"},
     "enabled": False},
    {"id": "suno-pack", "kind": "music", "label": "Suno pack (prompt + lyrics)",
     "method": "suno_pack", "provider": "", "paid": False, "enabled": True},

    {"id": "pollinations-video", "kind": "video", "label": "Pollinations video",
     "method": "openai_video", "provider": "pollinations", "model": "",
     "enabled": True},
    {"id": "slideshow", "kind": "video", "label": "Slideshow from images (offline)",
     "method": "slideshow", "provider": "", "paid": False, "enabled": True},
]


def backends() -> list[dict[str, Any]]:
    stored = settings.get("media_backends")
    if not stored:
        stored = [dict(b) for b in DEFAULT_BACKENDS]
        settings.set("media_backends", stored)
    return [dict(b) for b in stored if isinstance(b, dict) and b.get("id")]


def save_backends(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        bid = re.sub(r"[^a-z0-9-]", "-", str(item.get("id") or "").lower())[:40]
        if not bid or bid in seen or item.get("kind") not in KINDS:
            continue
        seen.add(bid)
        clean.append({**item, "id": bid})
    settings.set("media_backends", clean)
    return clean


def reset_backends() -> list[dict[str, Any]]:
    settings.set("media_backends", [dict(b) for b in DEFAULT_BACKENDS])
    return backends()


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS media (
            id TEXT PRIMARY KEY, kind TEXT, prompt TEXT, backend TEXT,
            path TEXT, mime TEXT, bytes INTEGER, created REAL,
            chat_id TEXT DEFAULT '', project TEXT DEFAULT '',
            meta TEXT DEFAULT '{}')""")
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _row(r: tuple) -> dict[str, Any]:
    keys = ("id", "kind", "prompt", "backend", "path", "mime", "bytes",
            "created", "chat_id", "project", "meta")
    d = dict(zip(keys, r))
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    d["url"] = f"/api/media/{d['id']}"
    return d


def list_media(kind: str = "", limit: int = 120, project: str = "") -> list[dict[str, Any]]:
    conn = connect()
    sql = "SELECT * FROM media"
    args: list[Any] = []
    where = []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if project:
        where.append("project = ?")
        args.append(project)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created DESC LIMIT ?"
    args.append(int(limit))
    with _lock:
        rows = conn.execute(sql, args).fetchall()
    return [_row(r) for r in rows]


def get(media_id: str) -> dict[str, Any] | None:
    conn = connect()
    with _lock:
        r = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    return _row(r) if r else None


def delete(media_id: str) -> bool:
    item = get(media_id)
    if not item:
        return False
    try:
        Path(item["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    conn = connect()
    with _lock:
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
        conn.commit()
    return True


def _store(kind: str, prompt: str, backend: str, data: bytes, mime: str,
           meta: dict[str, Any] | None = None) -> dict[str, Any]:
    from . import projects

    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if mime in ("application/octet-stream", "binary/octet-stream", ""):
        mime = _sniff(data) or {"image": "image/png", "speech": "audio/mpeg",
                                "music": "audio/mpeg",
                                "video": "video/mp4"}[kind]
    media_id = uuid.uuid4().hex[:12]
    folder = MEDIA_DIR / time.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:40].strip("-") or kind
    ext = EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    path = folder / f"{time.strftime('%d-%H%M%S')}-{slug}-{media_id[:4]}{ext}"
    path.write_bytes(data)

    project = projects.current_key()
    chat_id = projects.current_chat()
    try:
        # A copy in the workspace, so it shows in the chat's file list and
        # the code tools can work on it (project workspace when in a project).
        root = projects.workspace(project) if project else projects.general_workspace()
        dest = root / "media"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / path.name).write_bytes(data)
    except OSError:
        pass

    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (media_id, kind, prompt[:2000], backend, str(path), mime, len(data),
             time.time(), chat_id, project, json.dumps(meta or {})))
        conn.commit()
    return get(media_id) or {}


def _sniff(data: bytes) -> str:
    head = data[:16]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[:4] == b"GIF8":
        return "image/gif"
    return ""


# ---------------------------------------------------------------------------
# Resolving a backend to a URL + key
# ---------------------------------------------------------------------------

class Skip(Exception):
    """This backend cannot run here at all (not configured) - not a failure."""


class Failed(Exception):
    def __init__(self, text: str, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(text)
        self.status = status
        self.retry_after = retry_after


def _endpoint(backend: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from . import vault
    from .providers import custom

    headers: dict[str, str] = {}
    provider_key = backend.get("provider") or ""
    if provider_key:
        spec = custom.stored().get(provider_key)
        if not spec or not spec.get("base_url"):
            raise Skip(f"provider {provider_key!r} is not added")
        base = spec["base_url"].rstrip("/")
        headers.update(spec.get("headers") or {})
        token = vault.get(custom.key_name(provider_key))
        if not token:
            raise Skip(f"no API key saved for {provider_key}")
        headers["Authorization"] = f"Bearer {token}"
    else:
        base = str(backend.get("base_url") or "").rstrip("/")
    return base, headers


def is_paid(backend: dict[str, Any]) -> bool:
    if "paid" in backend and backend["paid"] is not None:
        return bool(backend["paid"])
    provider_key = backend.get("provider") or ""
    if not provider_key:
        return False
    from . import router
    return router.is_paid(provider_key, backend.get("model") or "*")


def _root(base: str) -> str:
    """https://gen.pollinations.ai/v1 -> https://gen.pollinations.ai"""
    return re.sub(r"/v1/?$", "", base.rstrip("/"))


def _fill(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, str):
        whole = re.fullmatch(r"\{(\w+)\}", template)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        return re.sub(r"\{(\w+)\}",
                      lambda m: str(values.get(m.group(1), m.group(0))), template)
    if isinstance(template, list):
        return [_fill(t, values) for t in template]
    if isinstance(template, dict):
        return {k: _fill(v, values) for k, v in template.items()
                if not (isinstance(v, str) and v == "{model}" and not values.get("model"))}
    return template


async def _check(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        from .providers.openai_compat import _retry_after_header
        body = resp.text[:400] if hasattr(resp, "text") else ""
        raise Failed(f"HTTP {resp.status_code}: {body}", resp.status_code,
                     _retry_after_header(resp.headers))


async def _download(client: httpx.AsyncClient, url: str,
                    headers: dict[str, str] | None = None) -> tuple[bytes, str]:
    if url.startswith("data:"):
        head, _, payload = url.partition(",")
        mime = head[5:].split(";")[0] or "application/octet-stream"
        return base64.b64decode(payload), mime
    resp = await client.get(url, headers=headers or {}, timeout=300.0)
    await _check(resp)
    return resp.content, resp.headers.get("content-type", "")


def _find_media(payload: Any) -> tuple[str, str]:
    """Dig a url or base64 blob out of whatever JSON a gateway returns."""
    if isinstance(payload, dict):
        for key in ("b64_json", "b64", "base64", "audio", "video_b64"):
            value = payload.get(key)
            if isinstance(value, str) and len(value) > 200:
                return "b64", value
        for key in ("url", "video_url", "audio_url", "image_url", "output",
                    "file", "uri"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("url")
            if isinstance(value, str) and value.startswith(("http", "data:")):
                return "url", value
        for value in payload.values():
            found = _find_media(value)
            if found[0]:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_media(value)
            if found[0]:
                return found
    return "", ""


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------

async def _pollinations_image(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    try:
        base, headers = _endpoint(b)
    except Skip:
        if b.get("provider"):
            raise
        base, headers = b.get("base_url", ""), {}
    params: dict[str, Any] = {"width": req.get("width") or 1024,
                              "height": req.get("height") or 1024,
                              "nologo": "true"}
    if b.get("model"):
        params["model"] = b["model"]
    if req.get("seed") is not None:
        params["seed"] = req["seed"]
    url = f"{_root(base)}/image/{quote(req['prompt'][:1500], safe='')}"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=headers,
                                timeout=TIMEOUT["image"])
        await _check(resp)
        mime = resp.headers.get("content-type", "")
        if "json" in mime or "text" in mime:
            raise Failed(f"expected an image, got {mime}: {resp.text[:200]}")
        return resp.content, mime


async def _openai_image(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    base, headers = _endpoint(b)
    body: dict[str, Any] = {"prompt": req["prompt"], "n": 1,
                            "size": f"{req.get('width') or 1024}x{req.get('height') or 1024}",
                            "response_format": "b64_json"}
    if b.get("model"):
        body["model"] = b["model"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{base}/images/generations", json=body,
                                 headers=headers, timeout=TIMEOUT["image"])
        await _check(resp)
        how, value = _find_media(resp.json())
        if how == "b64":
            return base64.b64decode(value), ""
        if how == "url":
            return await _download(client, value)
    raise Failed("the images endpoint returned no image")


async def _chat_image(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    base, headers = _endpoint(b)
    body = {"model": b.get("model") or "", "modalities": ["image", "text"],
            "messages": [{"role": "user", "content":
                          f"Generate an image: {req['prompt']}"}]}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{base}/chat/completions", json=body,
                                 headers=headers, timeout=TIMEOUT["image"])
        await _check(resp)
        data = resp.json()
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        for image in message.get("images") or []:
            url = (image.get("image_url") or {}).get("url") or image.get("url")
            if url:
                return await _download(client, url)
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                url = ((part or {}).get("image_url") or {}).get("url")
                if url:
                    return await _download(client, url)
        how, value = _find_media(message)
        if how == "b64":
            return base64.b64decode(value), ""
        if how == "url":
            return await _download(client, value)
    raise Failed("the model answered without an image (it may not generate images)",
                 400)


async def _openai_speech(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    base, headers = _endpoint(b)
    body: dict[str, Any] = {"input": req["prompt"][:4000],
                            "voice": req.get("voice") or b.get("voice") or "alloy",
                            "response_format": "mp3"}
    if b.get("model"):
        body["model"] = b["model"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{base}/audio/speech", json=body,
                                 headers=headers, timeout=TIMEOUT["speech"])
        await _check(resp)
        mime = resp.headers.get("content-type", "")
        if "json" in mime:
            how, value = _find_media(resp.json())
            if how == "b64":
                return base64.b64decode(value), ""
            if how == "url":
                return await _download(client, value)
            raise Failed("the speech endpoint returned JSON without audio")
        return resp.content, mime


async def _openai_video(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    base, headers = _endpoint(b)
    body: dict[str, Any] = {"prompt": req["prompt"]}
    if b.get("model"):
        body["model"] = b["model"]
    if req.get("duration"):
        body["duration"] = int(req["duration"])
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{base}/video/generations", json=body,
                                 headers=headers, timeout=TIMEOUT["video"])
        await _check(resp)
        mime = resp.headers.get("content-type", "")
        if mime.startswith("video/"):
            return resp.content, mime
        data = resp.json()
        deadline = time.time() + TIMEOUT["video"]
        while True:
            how, value = _find_media(data)
            if how == "b64":
                return base64.b64decode(value), ""
            if how == "url":
                return await _download(client, value, headers)
            job = data.get("id") if isinstance(data, dict) else None
            status = str((data or {}).get("status", "")).lower()
            if not job or status in ("failed", "error", "cancelled"):
                raise Failed(f"video job did not produce a file: {str(data)[:200]}")
            if time.time() > deadline:
                raise Failed("video job timed out", None)
            await asyncio.sleep(5)
            poll = await client.get(f"{base}/video/generations/{job}",
                                    headers=headers, timeout=60.0)
            await _check(poll)
            data = poll.json()


async def _http_post(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    base, headers = _endpoint(b)
    values = {**req, "model": b.get("model") or "",
              "lyrics": req.get("lyrics") or "", "duration": req.get("duration") or 30}
    body = _fill(b.get("body") or {"prompt": "{prompt}"}, values)
    path = str(b.get("path") or "")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{base}{path}", json=body, headers=headers,
                                 timeout=TIMEOUT.get(b["kind"], 300.0))
        await _check(resp)
        mime = resp.headers.get("content-type", "")
        if "json" not in mime:
            return resp.content, mime
        how, value = _find_media(resp.json())
        if how == "b64":
            return base64.b64decode(value), ""
        if how == "url":
            return await _download(client, value)
    raise Failed("the endpoint returned no media")


async def _gradio(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    space = str(b.get("space") or "").strip()
    if not space:
        raise Skip("no Space configured")
    try:
        from gradio_client import Client  # type: ignore
    except ImportError as exc:
        raise Skip("gradio_client is not installed (pip install gradio_client)") from exc
    from . import vault
    token = vault.get("hf_token") or None
    values = {**req, "lyrics": req.get("lyrics") or "",
              "duration": req.get("duration") or 30}
    args = _fill(b.get("args") or ["{prompt}"], values)

    def call() -> Any:
        client = Client(space, hf_token=token) if token else Client(space)
        return client.predict(*args, api_name=b.get("api_name") or None)

    try:
        result = await asyncio.wait_for(asyncio.to_thread(call),
                                        TIMEOUT.get(b["kind"], 300.0))
    except asyncio.TimeoutError as exc:
        raise Failed("the Space took too long") from exc
    except Exception as exc:
        text = str(exc)
        raise Failed(text[:300], 429 if "quota" in text.lower() else 503) from exc
    path = result[0] if isinstance(result, (list, tuple)) else result
    if isinstance(path, dict):
        path = path.get("path") or path.get("name") or path.get("url")
    if isinstance(path, str) and path.startswith("http"):
        async with httpx.AsyncClient(follow_redirects=True) as client:
            return await _download(client, path)
    if isinstance(path, str) and Path(path).is_file():
        data = Path(path).read_bytes()
        return data, mimetypes.guess_type(path)[0] or ""
    raise Failed(f"the Space returned something AEGIS cannot save: {str(result)[:160]}")


async def _local_tts(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    try:
        import pyttsx3  # type: ignore
    except ImportError as exc:
        raise Skip("pyttsx3 is not installed") from exc
    import tempfile

    target = Path(tempfile.mkdtemp(prefix="aegis-tts-")) / "speech.wav"

    def speak() -> None:
        engine = pyttsx3.init()
        wanted = str(req.get("voice") or "").lower()
        if wanted:
            for voice in engine.getProperty("voices") or []:
                if wanted in (voice.name or "").lower() or wanted in str(voice.languages).lower():
                    engine.setProperty("voice", voice.id)
                    break
        engine.save_to_file(req["prompt"][:6000], str(target))
        engine.runAndWait()

    try:
        await asyncio.wait_for(asyncio.to_thread(speak), 120)
    except Exception as exc:
        raise Failed(f"offline voice failed: {exc}") from exc
    if not target.is_file() or target.stat().st_size < 100:
        raise Failed("offline voice produced no audio")
    return target.read_bytes(), "audio/wav"


async def _suno_pack(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    """Always works: a paste-ready Suno pack written by the chat route."""
    prompt = req["prompt"]
    lyrics = req.get("lyrics") or ""
    ask = (
        "Write a Suno v4.5+ song pack. Output exactly these sections in "
        "Markdown:\n## Title\n## Style prompt (max 200 characters, comma-"
        "separated genre, instruments, BPM, vocal type, mood)\n## Lyrics (with "
        "[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro] tags)\n"
        "## Exclude styles\n\nBrief: " + prompt
        + (f"\n\nUse or adapt these lyrics:\n{lyrics}" if lyrics else ""))
    text = await quick_text(ask, system=(
        "You are a songwriter and Suno prompt engineer. Keep the user's "
        "language choice; mixing languages is welcome."))
    if not text.strip():
        text = f"## Style prompt\n{prompt}\n\n## Lyrics\n{lyrics or '(write here)'}\n"
    body = (f"# Suno pack\n\n*Paste the style prompt and lyrics into Suno "
            f"(Custom mode). No free music API answered, so this is the "
            f"ready-to-paste version.*\n\n{text.strip()}\n")
    return body.encode("utf-8"), "text/markdown"


async def _slideshow(b: dict[str, Any], req: dict[str, Any]) -> tuple[bytes, str]:
    """Video fallback: 4-6 generated stills, slow zoom, optional narration."""
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError as exc:
        raise Skip("imageio-ffmpeg is not installed") from exc
    import subprocess
    import tempfile

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    scenes_text = await quick_text(
        "Split this video idea into 4 to 6 short visual scene descriptions for "
        "an image generator, one per line, no numbering, no extra text:\n"
        + req["prompt"])
    scenes = [s.strip(" -*\t") for s in scenes_text.splitlines() if s.strip()][:6]
    if len(scenes) < 2:
        scenes = [req["prompt"]] * 4

    work = Path(tempfile.mkdtemp(prefix="aegis-video-"))
    stills: list[Path] = []
    for i, scene in enumerate(scenes):
        try:
            item = await generate("image", scene, width=1280, height=720,
                                  _index=False)
        except Exception:
            continue
        if item.get("_bytes"):
            p = work / f"s{i}.png"
            p.write_bytes(item["_bytes"])
            stills.append(p)
    if not stills:
        raise Failed("no images could be generated for the slideshow")

    seconds = max(2.0, float(req.get("duration") or 16) / len(stills))
    clips = []
    for i, still in enumerate(stills):
        clip = work / f"c{i}.mp4"
        frames = int(seconds * 25)
        cmd = [ffmpeg, "-y", "-loop", "1", "-i", str(still), "-vf",
               (f"scale=1920:1080,zoompan=z='min(zoom+0.0012,1.15)':d={frames}"
                f":s=1280x720:fps=25,format=yuv420p"),
               "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "veryfast",
               str(clip)]
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                timeout=180)
        if clip.is_file():
            clips.append(clip)
    if not clips:
        raise Failed("ffmpeg could not build the clips")
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), "utf-8")
    out = work / "video.mp4"
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
           "-c", "copy", str(out)]
    narration = str(req.get("narration") or "").strip()
    if narration:
        try:
            voice = await generate("speech", narration, _index=False)
            audio = work / ("voice" + (EXT.get(voice.get("mime", ""), ".mp3")))
            audio.write_bytes(voice["_bytes"])
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-shortest",
                   str(out)]
        except Exception:
            pass
    await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=300)
    if not out.is_file():
        raise Failed("ffmpeg could not join the clips")
    return out.read_bytes(), "video/mp4"


METHODS = {
    "pollinations_image": _pollinations_image,
    "openai_image": _openai_image,
    "chat_image": _chat_image,
    "openai_speech": _openai_speech,
    "openai_video": _openai_video,
    "http_post": _http_post,
    "gradio": _gradio,
    "local_tts": _local_tts,
    "suno_pack": _suno_pack,
    "slideshow": _slideshow,
}


# ---------------------------------------------------------------------------
# A short text answer from the default route - used by the fallbacks
# ---------------------------------------------------------------------------

async def quick_text(prompt: str, system: str = "") -> str:
    from . import providers

    key = settings.get("default_route")
    provider = providers.get(f"route:{key}") if key else None
    model = ""
    if provider is None:
        provider = providers.get(settings.get("default_provider") or "ollama")
        model = settings.get("default_model") or ""
    if provider is None:
        return ""
    messages = [{"role": "user", "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    out: list[str] = []
    try:
        async for event in provider.chat(messages, model):
            if event.get("type") == "delta":
                out.append(event.get("text", ""))
            elif event.get("type") == "error":
                break
    except Exception:
        return ""
    return "".join(out)


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def chain(kind: str, allow_paid: bool | None = None) -> list[dict[str, Any]]:
    if allow_paid is None:
        allow_paid = bool(settings.get("media_allow_paid", False))
    out = []
    for b in backends():
        if b.get("kind") != kind or not b.get("enabled", True):
            continue
        if not allow_paid and is_paid(b):
            continue
        out.append(b)
    return out


async def generate(kind: str, prompt: str, *, backend: str = "",
                   allow_paid: bool | None = None, _index: bool = True,
                   **req: Any) -> dict[str, Any]:
    """Make one piece of media, failing over across backends. Never raises
    for an ordinary outage - it raises only when every backend is spent."""
    from . import router

    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("a prompt is required")

    candidates = chain(kind, allow_paid)
    if backend:
        candidates = [b for b in backends() if b["id"] == backend] or candidates
    request = {"prompt": prompt, **req}
    tried: list[str] = []

    for b in candidates:
        health_key = f"media:{b['id']}"
        if router.book.peek(health_key, kind).resting:
            tried.append(f"{b.get('label', b['id'])}: resting")
            continue
        method = METHODS.get(b.get("method", ""))
        if method is None:
            tried.append(f"{b['id']}: unknown method {b.get('method')!r}")
            continue
        started = time.time()
        try:
            data, mime = await method(b, request)
        except Skip as exc:
            tried.append(f"{b.get('label', b['id'])}: {exc}")
            continue
        except Failed as exc:
            kind_ = router.classify(exc.status, str(exc))
            if kind_ in router.FAILOVER_KINDS or kind_ == router.UNKNOWN:
                router.book.record_failure(health_key, kind, kind_, str(exc),
                                           retry_after=exc.retry_after)
            tried.append(f"{b.get('label', b['id'])}: {str(exc)[:160]}")
            continue
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            router.book.record_failure(health_key, kind, router.TIMEOUT
                                       if isinstance(exc, httpx.HTTPError)
                                       else router.UNKNOWN, str(exc))
            tried.append(f"{b.get('label', b['id'])}: {type(exc).__name__}: {exc}"[:200])
            continue
        if not data:
            tried.append(f"{b.get('label', b['id'])}: empty result")
            continue
        router.book.record_success(health_key, kind,
                                   seconds=time.time() - started)
        if not _index:
            return {"_bytes": data, "mime": mime or _sniff(data),
                    "backend": b["id"]}
        item = _store(kind, prompt, b["id"], data, mime,
                      meta={"backend_label": b.get("label", ""),
                            "fallback": b.get("method") in ("suno_pack",
                                                            "slideshow",
                                                            "local_tts"),
                            **{k: v for k, v in req.items()
                               if k in ("width", "height", "voice", "duration",
                                        "seed", "style")}})
        item["tried"] = tried
        return item

    raise Failed("No backend could make this. " + ("; ".join(tried) or
                 f"No {kind} backends are enabled - see the Studio tab."))


def marker(item: dict[str, Any]) -> str:
    return MARKER.format(id=item["id"], mime=item.get("mime", ""))


def status() -> dict[str, Any]:
    from . import router
    out = []
    for b in backends():
        entry = dict(b)
        try:
            _endpoint(b) if b.get("provider") else None
            entry["ready"] = True
            entry["why"] = ""
        except Skip as exc:
            entry["ready"] = False
            entry["why"] = str(exc)
        if b.get("method") == "local_tts":
            try:
                import pyttsx3  # noqa: F401
            except ImportError:
                entry["ready"], entry["why"] = False, "pip install pyttsx3"
        if b.get("method") == "slideshow":
            try:
                import imageio_ffmpeg  # noqa: F401
            except ImportError:
                entry["ready"], entry["why"] = False, "pip install imageio-ffmpeg"
        if b.get("method") == "gradio" and not b.get("space"):
            entry["ready"], entry["why"] = False, "set a Space id"
        entry["paid"] = is_paid(b)
        health = router.book.peek(f"media:{b['id']}", b["kind"])
        entry["resting"] = health.rest_remaining if health.resting else 0
        entry["last_error"] = health.last_error
        out.append(entry)
    return {"backends": out, "methods": sorted(METHODS),
            "allow_paid": bool(settings.get("media_allow_paid", False)),
            "kinds": list(KINDS)}


# ---------------------------------------------------------------------------
# Tools the agent can call
# ---------------------------------------------------------------------------

def tools() -> list[Any]:
    from .tools.base import READ, Tool

    def wrap(kind: str):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            prompt = str(args.pop("prompt", "") or args.pop("text", "") or "")
            count = max(1, min(int(args.pop("count", 1) or 1), 4))
            made, errors = [], []
            for i in range(count if kind == "image" else 1):
                try:
                    if count > 1 and "seed" not in args:
                        args["seed"] = int(time.time()) % 100000 + i
                    item = await generate(kind, prompt, **args)
                    made.append(item)
                except Failed as exc:
                    errors.append(str(exc))
                except ValueError as exc:
                    return {"text": str(exc), "error": True}
            if not made:
                return {"text": errors[0] if errors else "Nothing was made.",
                        "error": True}
            lines = []
            for item in made:
                note = " (fallback: " + item["meta"].get("backend_label", "") + ")" \
                    if item.get("meta", {}).get("fallback") else ""
                lines.append(f"{marker(item)} saved to {item['path']}{note}")
            lines.append("It is already shown to the user in the chat - do not "
                         "paste the file path again unless asked.")
            return {"text": "\n".join(lines)}
        return handler

    def t(name: str, desc: str, props: dict[str, Any], required: list[str],
          kind: str) -> Tool:
        return Tool(name=f"media__{name}", raw_name=name, description=desc,
                    input_schema={"type": "object", "properties": props,
                                  "required": required},
                    server="media", origin="builtin", risk=READ,
                    handler=wrap(kind))

    return [
        t("generate_image",
          "Create an image from a text prompt (free backends first, with "
          "automatic failover). Write a rich visual prompt: subject, style, "
          "lighting, composition. Use width/height for aspect ratio "
          "(e.g. 1280x720 thumbnail, 1024x1024 square, 768x1344 portrait).",
          {"prompt": {"type": "string"}, "width": {"type": "integer"},
           "height": {"type": "integer"},
           "count": {"type": "integer", "description": "1-4 variations"},
           "seed": {"type": "integer"}}, ["prompt"], "image"),
        t("generate_speech",
          "Turn text into spoken audio (voice-over, narration, reading aloud).",
          {"text": {"type": "string"},
           "voice": {"type": "string", "description": "e.g. nova, alloy, echo, "
                                                      "or a Windows voice name"}},
          ["text"], "speech"),
        t("generate_music",
          "Create a song or instrumental. When no free music model answers, "
          "you get a ready-to-paste Suno pack (style prompt + lyrics) instead.",
          {"prompt": {"type": "string",
                      "description": "genre, mood, instruments, BPM, language"},
           "lyrics": {"type": "string"},
           "duration": {"type": "integer", "description": "seconds"}},
          ["prompt"], "music"),
        t("generate_video",
          "Create a short video from a description. Falls back to an animated "
          "slideshow of generated images (with optional narration) when no "
          "video model is available.",
          {"prompt": {"type": "string"},
           "duration": {"type": "integer", "description": "seconds, default 16"},
           "narration": {"type": "string",
                         "description": "optional voice-over text"}},
          ["prompt"], "video"),
    ]
