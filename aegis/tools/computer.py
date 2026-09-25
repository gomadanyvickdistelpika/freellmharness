"""Computer use: let a model see and drive this machine.

**Not for browsing.** Anything on the web goes through the browser tools, which
drive AEGIS's own Chromium at page level - faster, cheaper, and far more
reliable than aiming a mouse at a screenshot of a browser window. Computer use
is for the desktop itself: an installer, Explorer, a native app, a VM console,
something with no web interface at all. It is also what to use when the user
explicitly asks for their own screen or their own Chrome window to be driven.

Off by default. It arms per conversation, and every action that touches the
mouse or keyboard is classified WRITE, so under the default policy each one
asks before it runs.

Coordinate handling is the part that usually goes wrong. Screenshots are scaled
down before they go to the model (a 4K screenshot costs a fortune in tokens and
most vision models downscale it anyway), so the model returns coordinates in the
*scaled* image. Every click maps those back to real screen pixels using the
scale factor from the last screenshot. Get this wrong and the agent clicks 300
pixels off and nobody can work out why.

Two safety hatches that are deliberately not configurable:
  * pyautogui's FAILSAFE stays on - slam the mouse into the top-left corner and
    the next action raises instead of continuing.
  * There is no "close window", "delete file" or shell tool here. Anything
    destructive is the job of an MCP server you chose to enable.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass
from typing import Any

from .base import READ, WRITE, Tool

MAX_WIDTH = 1280          # what we scale screenshots down to
JPEG_QUALITY = 72

_state: dict[str, Any] = {"scale": 1.0, "screen": (0, 0), "shot": (0, 0)}


@dataclass
class Capability:
    available: bool
    detail: str
    screen: tuple[int, int] = (0, 0)
    hint: str = ""


def _import_pyautogui():
    import pyautogui                                   # noqa: PLC0415
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def capability() -> Capability:
    """What this machine can actually do, checked rather than assumed."""
    try:
        from PIL import ImageGrab                      # noqa: F401, PLC0415
    except ImportError:
        return Capability(False, "Pillow is not installed",
                          hint="pip install pillow pyautogui")
    try:
        pg = _import_pyautogui()
        size = pg.size()
        return Capability(True, "Screenshot, mouse and keyboard available",
                          screen=(int(size.width), int(size.height)))
    except ImportError:
        return Capability(False, "pyautogui is not installed",
                          hint="pip install pyautogui")
    except Exception as exc:
        # Headless session, no display, locked workstation.
        return Capability(False, f"No usable display: {type(exc).__name__}: {exc}",
                          hint="Computer use needs an interactive desktop session.")


def enabled() -> bool:
    from ..config import settings
    return bool(settings.get("computer_use_enabled", True))


# ---------------------------------------------------------------------------
# Actions (all run in a thread - pyautogui is blocking)
# ---------------------------------------------------------------------------

def _screenshot_sync() -> dict[str, Any]:
    from PIL import Image, ImageGrab

    image = ImageGrab.grab(all_screens=False)
    real_w, real_h = image.size
    scale = min(1.0, MAX_WIDTH / real_w) if real_w else 1.0
    if scale < 1.0:
        image = image.resize((int(real_w * scale), int(real_h * scale)),
                             Image.LANCZOS)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    data = base64.b64encode(buffer.getvalue()).decode()

    _state["scale"] = scale
    _state["screen"] = (real_w, real_h)
    _state["shot"] = image.size

    return {
        "text": (f"Screenshot taken. The image below is {image.size[0]}x"
                 f"{image.size[1]}, the real screen is {real_w}x{real_h}. "
                 f"Give click coordinates in the image's coordinate space - "
                 f"they are scaled back automatically."),
        "images": [("image/jpeg", data)],
    }


def _to_screen(x: float, y: float) -> tuple[int, int]:
    """Map image coordinates back to real screen pixels."""
    scale = float(_state.get("scale") or 1.0) or 1.0
    real_w, real_h = _state.get("screen") or (0, 0)
    sx, sy = int(round(x / scale)), int(round(y / scale))
    if real_w and real_h:                      # never click off-screen
        sx = max(0, min(sx, real_w - 1))
        sy = max(0, min(sy, real_h - 1))
    return sx, sy


def _click_sync(x: float, y: float, button: str = "left",
                clicks: int = 1) -> dict[str, Any]:
    pg = _import_pyautogui()
    sx, sy = _to_screen(x, y)
    pg.click(sx, sy, clicks=clicks, interval=0.08,
             button=button if button in ("left", "right", "middle") else "left")
    return {"text": f"Clicked {button} at screen ({sx}, {sy}) "
                    f"[image ({x:.0f}, {y:.0f})]."}


def _move_sync(x: float, y: float) -> dict[str, Any]:
    pg = _import_pyautogui()
    sx, sy = _to_screen(x, y)
    pg.moveTo(sx, sy, duration=0.15)
    return {"text": f"Moved the pointer to ({sx}, {sy})."}


def _type_sync(text: str) -> dict[str, Any]:
    pg = _import_pyautogui()
    pg.write(text, interval=0.012)
    return {"text": f"Typed {len(text)} characters."}


def _key_sync(keys: str) -> dict[str, Any]:
    pg = _import_pyautogui()
    parts = [k.strip().lower() for k in keys.replace("-", "+").split("+") if k.strip()]
    if not parts:
        return {"text": "No key given.", "error": True}
    if len(parts) == 1:
        pg.press(parts[0])
    else:
        pg.hotkey(*parts)
    return {"text": f"Pressed {'+'.join(parts)}."}


def _scroll_sync(amount: int, x: float | None = None,
                 y: float | None = None) -> dict[str, Any]:
    pg = _import_pyautogui()
    if x is not None and y is not None:
        sx, sy = _to_screen(x, y)
        pg.moveTo(sx, sy)
    pg.scroll(int(amount))
    return {"text": f"Scrolled {amount:+d} clicks."}


def _drag_sync(x1: float, y1: float, x2: float, y2: float) -> dict[str, Any]:
    pg = _import_pyautogui()
    ax, ay = _to_screen(x1, y1)
    bx, by = _to_screen(x2, y2)
    pg.moveTo(ax, ay)
    pg.dragTo(bx, by, duration=0.4, button="left")
    return {"text": f"Dragged from ({ax}, {ay}) to ({bx}, {by})."}


def _cursor_sync() -> dict[str, Any]:
    pg = _import_pyautogui()
    pos = pg.position()
    return {"text": f"Pointer is at screen ({pos.x}, {pos.y})."}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _wrap(fn, **fixed):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, **{**fixed, **args})
        except Exception as exc:
            name = type(exc).__name__
            if name == "FailSafeException":
                return {"text": "Stopped: the pointer hit the fail-safe corner.",
                        "error": True}
            return {"text": f"{name}: {exc}", "error": True}
    return handler


def _num(desc: str) -> dict[str, Any]:
    return {"type": "number", "description": desc}


def tools() -> list[Tool]:
    """The computer-use tool set. Empty unless enabled and actually available."""
    if not enabled():
        return []
    cap = capability()
    if not cap.available:
        return []

    def tool(name: str, desc: str, schema: dict[str, Any], risk: str, handler) -> Tool:
        return Tool(name=f"computer__{name}", raw_name=name, description=desc,
                    input_schema=schema, server="computer", origin="builtin",
                    risk=risk, handler=handler)

    coords = {"x": _num("X in the last screenshot's coordinate space"),
              "y": _num("Y in the last screenshot's coordinate space")}

    return [
        tool("screenshot",
             "Take a screenshot of THIS USER'S DESKTOP. Do this before any "
             "click so you know where things are, and again afterwards to "
             "check what changed. For a web page use the browser tools "
             "instead - they read the page directly rather than looking at a "
             "picture of it, and they do not touch the user's screen.",
             {"type": "object", "properties": {}},
             READ, _wrap(_screenshot_sync)),

        tool("cursor_position", "Report where the mouse pointer currently is.",
             {"type": "object", "properties": {}},
             READ, _wrap(_cursor_sync)),

        tool("click", "Click at a point from the last screenshot.",
             {"type": "object",
              "properties": {**coords,
                             "button": {"type": "string",
                                        "enum": ["left", "right", "middle"],
                                        "description": "Default left"}},
              "required": ["x", "y"]},
             WRITE, _wrap(_click_sync)),

        tool("double_click", "Double-click at a point from the last screenshot.",
             {"type": "object", "properties": coords, "required": ["x", "y"]},
             WRITE, _wrap(_click_sync, clicks=2)),

        tool("move", "Move the pointer without clicking, to reveal hover states.",
             {"type": "object", "properties": coords, "required": ["x", "y"]},
             WRITE, _wrap(_move_sync)),

        tool("type", "Type text at the current focus. Click the field first.",
             {"type": "object",
              "properties": {"text": {"type": "string", "description": "Text to type"}},
              "required": ["text"]},
             WRITE, _wrap(_type_sync)),

        tool("key",
             "Press a key or a combination, e.g. 'enter', 'tab', 'ctrl+c', "
             "'alt+tab', 'win+r'.",
             {"type": "object",
              "properties": {"keys": {"type": "string",
                                      "description": "Key or combo joined by +"}},
              "required": ["keys"]},
             WRITE, _wrap(_key_sync)),

        tool("scroll", "Scroll the wheel. Positive scrolls up, negative down.",
             {"type": "object",
              "properties": {"amount": {"type": "integer",
                                        "description": "Wheel clicks, e.g. -3"},
                             **{k: {**v, "description": v["description"] + " (optional)"}
                                for k, v in coords.items()}},
              "required": ["amount"]},
             WRITE, _wrap(_scroll_sync)),

        tool("drag", "Press at one point, drag to another, release.",
             {"type": "object",
              "properties": {"x1": _num("Start X"), "y1": _num("Start Y"),
                             "x2": _num("End X"), "y2": _num("End Y")},
              "required": ["x1", "y1", "x2", "y2"]},
             WRITE, _wrap(_drag_sync)),
    ]


def status() -> dict[str, Any]:
    cap = capability()
    return {"enabled": enabled(), "available": cap.available,
            "detail": cap.detail, "hint": cap.hint,
            "screen": {"width": cap.screen[0], "height": cap.screen[1]},
            "tool_count": len(tools()),
            "note": ("Needs a vision-capable model. Local small models cannot "
                     "read a screenshot - route computer use to Claude or GPT.")}
