"""Needle: an offline "fast intent" step in front of the model route.

Needle (Cactus Compute, Apache-2.0) is a ~26-45M parameter tool-calling model
that runs on the CPU in a few milliseconds and never touches the network once
its engine and weights are cached. It cannot chat, reason or write - it only
turns a short command into one structured tool call.

So AEGIS uses it for exactly that: before a short plain-language message goes
to a cloud or local LLM, Needle checks whether it is really one of AEGIS's own
commands ("open youtube.com", "turn boost off", "switch to the job hunt
project", "make an image of a lighthouse at dusk"). When it is - and only when
every gate below agrees - the message becomes the matching slash command and
runs through the exact path a typed slash command takes. Everything else goes
to the model route unchanged.

Fail closed, always:

  * Needle not installed, not downloaded yet, or erroring  -> no match.
  * Long, multi-line or code-looking text                  -> not even asked.
  * No tool call, several calls, an unknown tool           -> no match.
  * Confidence missing or under the threshold              -> no match.
  * Needle flags an argument as ungrounded or negated      -> no match.
  * A name or web address that is not in what you typed    -> no match.

Deliberately NOT exposed to Needle: /undo (changes files) and /computer
(drives the desktop). Those stay things you type on purpose.

Install once, inside the AEGIS venv:
    pip install cactus-needle==3.0.4
(3.0.5 asks Hugging Face for an engine build, 3.0.2, that Cactus has not
published yet, so it fails with a 404. 3.0.4 uses the published 3.0.1 engine.)
The first use downloads the engine DLL and the weights (~30 MB) from Hugging
Face into %USERPROFILE%\\.cache\\cactus-needle; after that it works offline.
Needle's anonymous usage telemetry is switched off before it is imported.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from .config import settings

DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_MAX_CHARS = 160
ARG_CAP = 300

SYSTEM = ("You route short commands for AEGIS, a desktop AI assistant. Call a "
          "tool only when the user is clearly asking AEGIS to do that exact "
          "thing. Copy names, addresses and descriptions word for word. For "
          "questions, requests to write or explain something, or anything "
          "else, call no tool.")


def _schema(name: str, description: str, props: dict[str, Any] | None = None,
            required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {},
                           "required": required or []}}


def _text(desc: str, max_len: int = 200) -> dict[str, Any]:
    return {"type": "string", "description": desc, "minLength": 1,
            "maxLength": max_len}


# name -> (schema, slash builder, grounded args)
# "grounded" args must literally appear in what was typed, so Needle cannot
# invent a project, an agent or a web address.
COMMANDS: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], str], tuple[str, ...]]] = {
    "open_browser": (
        _schema("open_browser", "Open the browser panel, optionally at a web "
                "address the user names.",
                {"url": _text("The web address or site name, copied word for word.")}),
        lambda a: ("/browser " + _url(a["url"])) if a.get("url") else "/browser",
        ("url",)),
    "new_chat": (
        _schema("new_chat", "Start a new, empty chat."),
        lambda a: "/new", ()),
    "set_boost": (
        _schema("set_boost", "Turn Boost (plan plus second-model review) on, "
                "off or back to auto.",
                {"mode": {"type": "string", "enum": ["on", "off", "auto"]}},
                ["mode"]),
        lambda a: f"/boost {a['mode']}", ()),
    "switch_project": (
        _schema("switch_project", "Switch to one of the user's projects.",
                {"name": _text("The project name, copied word for word.", 60)},
                ["name"]),
        lambda a: f"/project {a['name']}", ("name",)),
    "switch_agent": (
        _schema("switch_agent", "Switch the chat to one of the user's agents, "
                "such as the coder or the job hunter.",
                {"name": _text("The agent name, copied word for word.", 60)},
                ["name"]),
        lambda a: f"/agent {_agent_name(a['name'])}", ("name",)),
    "make_image": (
        _schema("make_image", "Generate a picture from a description.",
                {"prompt": _text("What the picture should show.")}, ["prompt"]),
        lambda a: f"/image {a['prompt']}", ()),
    "make_music": (
        _schema("make_music", "Make a song, beat or Suno prompt pack.",
                {"description": _text("What the music should be.")},
                ["description"]),
        lambda a: f"/music {a['description']}", ()),
    "make_video": (
        _schema("make_video", "Make a short video.",
                {"description": _text("What the video should show.")},
                ["description"]),
        lambda a: f"/video {a['description']}", ()),
    "read_aloud": (
        _schema("read_aloud", "Turn a given piece of text into speech.",
                {"text": _text("The exact text to speak.")}, ["text"]),
        lambda a: f"/voice {a['text']}", ()),
    "web_search": (
        _schema("web_search", "Research something on the web with sources.",
                {"query": _text("What to search for.")}, ["query"]),
        lambda a: f"/search {a['query']}", ()),
    "schedule_task": (
        _schema("schedule_task", "Create a recurring or timed task, such as a "
                "daily job search at 8am.",
                {"description": _text("What to do and when, word for word.")},
                ["description"]),
        lambda a: f"/schedule {a['description']}", ()),
    "show_files": (
        _schema("show_files", "Show the workspace files available to download."),
        lambda a: "/files", ()),
    "toggle_read_aloud": (
        _schema("toggle_read_aloud", "Switch reading replies aloud on or off."),
        lambda a: "/speak", ()),
    "toggle_hands_free": (
        _schema("toggle_hands_free", "Switch hands-free voice mode on or off."),
        lambda a: "/talk", ()),
}

SCHEMAS = [entry[0] for entry in COMMANDS.values()]

# Evidence gate: Needle always picks *some* tool when it can, so each command
# also needs one of its own words in what you typed. Real-engine finding on
# a Windows laptop (cactus-needle 3.0.4): "write me a cover letter for a VMware
# support job" came back as make_video. No "video" word, no video.
ANCHORS: dict[str, re.Pattern[str]] = {k: re.compile(v, re.IGNORECASE) for k, v in {
    "open_browser": r"\b(open|go to|goto|browse|visit|launch|load|website|site|"
                    r"page|browser)\b|https?://|\w\.(com|ie|org|net|io|ai|co|dev|fr)\b",
    "new_chat": r"\b(new|fresh|clear|another|blank)\b.*\b(chat|conversation|thread)\b"
                r"|\bstart over\b",
    "set_boost": r"\bboost",
    "switch_project": r"\bproject",
    "switch_agent": r"\b(agent|coder|assistant|persona)\b",
    "make_image": r"\b(image|picture|pic|photo|draw|drawing|illustrat\w*|logo|"
                  r"thumbnail|poster|artwork|wallpaper|icon|sketch|paint\w*)\b",
    "make_music": r"\b(song|music|beat|track|instrumental|suno|melody|tune|"
                  r"coup[eé]|soukous|afrobeat|rumba|dancehall)\b",
    "make_video": r"\b(video|clip|film|movie|animation|animate|reel|slideshow)\b",
    "read_aloud": r"\b(read|say|speak|pronounce|aloud|out loud|voice ?over|tts)\b",
    # Not bare "search": "summarise this week's job search" is not a web search.
    "web_search": r"\b(search (the web|online|for|google|up)|google|look up|lookup|"
                  r"research|on the web|online|browse for|latest news)\b",
    "schedule_task": r"\b(schedule|every|daily|weekly|weekday|each (day|morning|week)|"
                     r"remind|recurring|at \d|tomorrow|tonight|cron)\b",
    "show_files": r"\b(files?|downloads?|workspace|outputs?)\b",
    "toggle_read_aloud": r"\b(read (replies|answers|responses|it) (aloud|out)|"
                         r"(speak|voice|read aloud|reading aloud)\b.*\b(on|off|mode|replies))",
    "toggle_hands_free": r"\b(hands[- ]?free|talk mode|voice mode|conversation mode)\b",
}.items()}

_CODEISH = re.compile(r"(```|\bdef \w+\(|\bimport \w+|[{};]\s*$|Traceback)")


def _agent_name(raw: str) -> str:
    """/agent takes ONE word and sends the rest as a message, so "coder agent"
    would switch to the coder and then send the word "agent" to the model.
    Real-engine finding: "switch to the coder agent" -> name "coder agent"."""
    words = [w for w in _clean(raw).lower().split()
             if w not in {"the", "my", "a", "an", "agent", "assistant", "mode"}]
    return words[0] if words else ""


def _url(raw: str) -> str:
    raw = _clean(raw)
    if not raw:
        return ""
    if "://" not in raw and "." not in raw:
        raw = raw.replace(" ", "") + ".com"      # "open youtube" -> youtube.com
    return raw if "://" in raw else "https://" + raw


def _url_label(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    host = host.lower().removeprefix("www.")
    return host.split(".")[0] if host else ""


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:ARG_CAP]


# ---------------------------------------------------------------------------
# The engine: loaded lazily, once, and only ever used from one thread at a time
# (Needle's native engine keeps one active context per process).
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.agent: Any = None
        self.error = ""
        self.loaded_at = 0.0
        self.load_seconds = 0.0
        self.version = ""
        self.failed_at = 0.0

    def reset(self) -> None:
        with self.lock:
            if self.agent is not None:
                try:
                    self.agent.close()
                except Exception:
                    pass
            self.agent, self.error, self.loaded_at = None, "", 0.0
            self.failed_at = 0.0


engine = _Engine()


def installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("needle") is not None
    except (ImportError, ValueError):
        return False


def _default_factory() -> Any:
    os.environ.setdefault("NEEDLE_TELEMETRY", "0")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    import needle  # noqa: WPS433 - optional dependency, imported on demand

    engine.version = getattr(needle, "__version__", "")
    weights = str(settings.get("needle_weights") or "").strip() or None
    kwargs = {"tools": SCHEMAS, "system": SYSTEM, "weights": weights}
    import inspect
    if "stateless" in inspect.signature(needle.Needle.__init__).parameters:
        kwargs["stateless"] = True       # 3.0.5+; older builds are reset per call
    return needle.Needle(**kwargs)


# Tests swap this for a fake; nothing else should.
factory: Callable[[], Any] = _default_factory


RETRY_AFTER = 300     # after a failed load, don't retry on every message


def _load_locked(force: bool = False) -> Any:
    if engine.agent is not None:
        return engine.agent
    if not force and engine.failed_at and time.time() - engine.failed_at < RETRY_AFTER:
        raise RuntimeError(engine.error or "Needle failed to load recently")
    started = time.perf_counter()
    try:
        engine.agent = factory()
    except Exception as exc:
        engine.failed_at = time.time()
        engine.error = f"{type(exc).__name__}: {exc}"[:400]
        raise
    engine.failed_at = 0.0
    engine.load_seconds = round(time.perf_counter() - started, 2)
    engine.loaded_at = time.time()
    engine.error = ""
    return engine.agent


def _complete_sync(text: str) -> dict[str, Any]:
    with engine.lock:
        agent = _load_locked()
        # Every message is independent: never let one command colour the next.
        reset = getattr(agent, "reset", None)
        if callable(reset):
            reset()
        started = time.perf_counter()
        response = agent.complete(text, max_new_tokens=128)
        response = dict(response or {})
        response["_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return response


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return bool(settings.get("needle_enabled", True))


def threshold() -> float:
    try:
        value = float(settings.get("needle_min_confidence", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_CONFIDENCE
    return min(max(value, 0.05), 0.99)


def status() -> dict[str, Any]:
    return {"installed": installed(), "enabled": enabled(),
            "loaded": engine.agent is not None, "error": engine.error,
            "version": engine.version, "load_seconds": engine.load_seconds,
            "min_confidence": threshold(),
            "max_chars": int(settings.get("needle_max_chars", DEFAULT_MAX_CHARS)),
            "commands": sorted(COMMANDS)}


def pregate(text: str) -> str:
    """Why this text should not even be shown to Needle ('' means go ahead)."""
    if not enabled():
        return "Needle is switched off"
    text = (text or "").strip()
    if not text:
        return "empty"
    if text.startswith("/"):
        return "already a slash command"
    if "\n" in text:
        return "more than one line"
    if len(text) > int(settings.get("needle_max_chars", DEFAULT_MAX_CHARS)):
        return "too long for a command"
    if _CODEISH.search(text):
        return "looks like code"
    return ""


def interpret(text: str, response: dict[str, Any]) -> dict[str, Any]:
    """Turn Needle's envelope into a slash command, or explain why not."""
    base = {"matched": False, "ms": response.get("_ms"),
            "confidence": response.get("confidence")}
    calls = response.get("function_calls") or []
    if response.get("type") not in (None, "call") or not calls:
        return {**base, "reason": "not a command"}
    if len(calls) != 1:
        return {**base, "reason": "more than one action - left to the model"}
    call = calls[0] or {}
    name = str(call.get("name") or "")
    args = call.get("arguments") or {}
    base.update(tool=name, args=args)
    if name not in COMMANDS:
        return {**base, "reason": f"unknown tool {name!r}"}
    confidence = response.get("confidence")
    if confidence is None:
        return {**base, "reason": "no confidence score - not acting on it"}
    if float(confidence) < threshold():
        return {**base, "reason": f"confidence {float(confidence):.2f} under "
                                  f"{threshold():.2f}"}
    validation = response.get("validation") or {}
    if validation.get("ungrounded") or validation.get("negation"):
        return {**base, "reason": "Needle flagged its own answer"}

    schema, build, grounded = COMMANDS[name]
    if not isinstance(args, dict):
        return {**base, "reason": "arguments were not an object"}
    clean = {k: _clean(v) for k, v in args.items()
             if k in schema["parameters"]["properties"]}
    for key in schema["parameters"]["required"]:
        if not clean.get(key):
            return {**base, "reason": f"missing {key}"}
    enum = schema["parameters"]["properties"].get("mode", {}).get("enum")
    if enum and clean.get("mode") not in enum:
        return {**base, "reason": "mode not one of " + ", ".join(enum)}
    anchor = ANCHORS.get(name)
    if anchor is not None and not anchor.search(text):
        return {**base, "reason": f"{name} but none of its words are in what "
                                  f"you typed"}
    lowered = text.lower()
    for key in grounded:
        value = clean.get(key, "")
        if not value:
            continue
        probe = _url_label(value) if key == "url" else value.lower()
        if not probe or probe not in lowered:
            return {**base, "reason": f"{key} {value!r} is not in what you typed"}
    return {**base, "matched": True, "args": clean, "slash": build(clean).strip(),
            "reason": "ok"}


async def detect(text: str) -> dict[str, Any]:
    """Main entry point: {'matched': bool, 'slash': '/…', 'reason': …}."""
    why = pregate(text)
    if why:
        return {"matched": False, "skipped": True, "reason": why}
    if factory is _default_factory and not installed():
        return {"matched": False, "skipped": True,
                "reason": "Needle is not installed (pip install cactus-needle==3.0.4)"}
    try:
        response = await asyncio.to_thread(_complete_sync, text.strip())
    except Exception as exc:                  # never break sending a message
        if engine.agent is not None or not engine.error:
            engine.error = f"{type(exc).__name__}: {exc}"[:400]
        return {"matched": False, "reason": "Needle error: " + engine.error}
    return interpret(text, response)


async def warmup() -> dict[str, Any]:
    """Download (first time) and load the engine now rather than on first use."""
    if factory is _default_factory and not installed():
        return {"ok": False, "error": "Needle is not installed. In the AEGIS "
                                      "folder run: .venv\\Scripts\\python.exe -m pip "
                                      "install cactus-needle==3.0.4"}
    try:
        await asyncio.to_thread(_warm_sync)
    except Exception as exc:
        engine.error = engine.error or f"{type(exc).__name__}: {exc}"[:400]
        return {"ok": False, "error": engine.error}
    return {"ok": True, **status()}


def _warm_sync() -> None:
    with engine.lock:
        _load_locked(force=True)       # the button always tries again
