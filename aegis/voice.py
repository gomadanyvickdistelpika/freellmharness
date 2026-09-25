"""Voice: speak to AEGIS, and let it speak back.

Speech-to-text walks a short list of free transcription backends, the same
failover idea as everything else:

    groq          whisper-large-v3-turbo on your Groq key (free tier, very fast)
    pollinations  /v1/audio/transcriptions on your Pollinations key
    openai        whisper-1 on an OpenAI key, if you have one (paid)
    local         faster-whisper on this PC, if installed (offline, slower)

If none answers, the browser's own speech recognition is used by the page
instead - so the mic button always does something.

Text-to-speech for replies is done by the browser/Windows voices (free,
offline, instant). The Studio's speech backends are for making audio files.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import settings

DEFAULT_STT = [
    {"provider": "groq", "model": "whisper-large-v3-turbo"},
    {"provider": "pollinations", "model": "whisper-1"},
    {"provider": "openai", "model": "whisper-1"},
    {"provider": "local", "model": "base"},
]


def backends() -> list[dict[str, Any]]:
    stored = settings.get("stt_backends")
    return [dict(b) for b in (stored or DEFAULT_STT) if isinstance(b, dict)]


def _endpoint(provider: str) -> tuple[str, str] | None:
    from . import vault
    from .providers import custom
    if provider == "openai":
        key = vault.get("openai_api_key")
        return ("https://api.openai.com/v1", key) if key else None
    spec = custom.stored().get(provider)
    if not spec or not spec.get("base_url"):
        return None
    key = vault.get(custom.key_name(provider))
    return (spec["base_url"].rstrip("/"), key or "") if key else None


async def _remote(base: str, key: str, model: str, data: bytes, filename: str,
                  language: str) -> str:
    files = {"file": (filename, data, "application/octet-stream")}
    form = {"model": model}
    if language:
        form["language"] = language
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base}/audio/transcriptions", files=files,
                                 data=form, headers={"Authorization": f"Bearer {key}"},
                                 timeout=120.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return str(resp.json().get("text", "")).strip()
    except ValueError:
        return resp.text.strip()


async def _local(model: str, data: bytes, filename: str, language: str) -> str:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise LookupError("faster-whisper is not installed") from exc
    suffix = Path(filename).suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name

    def run() -> str:
        engine = WhisperModel(model or "base", device="cpu", compute_type="int8")
        segments, _ = engine.transcribe(path, language=language or None)
        return " ".join(s.text.strip() for s in segments).strip()

    return await asyncio.to_thread(run)


async def transcribe(data: bytes, filename: str = "speech.webm",
                     language: str = "") -> dict[str, Any]:
    tried = []
    for backend in backends():
        provider = backend.get("provider", "")
        model = backend.get("model", "")
        try:
            if provider == "local":
                text = await _local(model, data, filename, language)
            else:
                found = _endpoint(provider)
                if not found:
                    tried.append(f"{provider}: not set up")
                    continue
                text = await _remote(found[0], found[1], model, data, filename, language)
        except LookupError as exc:
            tried.append(f"{provider}: {exc}")
            continue
        except Exception as exc:
            tried.append(f"{provider}: {str(exc)[:120]}")
            continue
        if text:
            return {"ok": True, "text": text, "backend": f"{provider}/{model}",
                    "tried": tried}
        tried.append(f"{provider}: empty")
    return {"ok": False, "error": "No transcription backend answered. "
                                  + "; ".join(tried), "tried": tried}


def tools() -> list[Any]:
    from .tools.base import READ, Tool

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from .tools.files import OutsideFence, resolve
        try:
            path = resolve(str(args.get("path") or ""))
        except OutsideFence as exc:
            return {"text": f"Refused: {exc}", "error": True}
        if not path.is_file():
            return {"text": f"{path} is not a file.", "error": True}
        result = await transcribe(path.read_bytes(), path.name,
                                  str(args.get("language") or ""))
        if not result["ok"]:
            return {"text": result["error"], "error": True}
        return {"text": f"[transcribed by {result['backend']}]\n{result['text']}"}

    return [Tool(
        name="voice__transcribe", raw_name="transcribe",
        description="Transcribe an audio or video file in the workspace (mp3, wav, "
                    "m4a, webm, mp4…) to text, e.g. a voice note or a meeting.",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string"},
            "language": {"type": "string", "description": "e.g. fr or en (optional)"}},
            "required": ["path"]},
        server="media", origin="builtin", risk=READ, handler=handler)]
