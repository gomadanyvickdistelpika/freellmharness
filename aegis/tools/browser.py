"""Browser use: page-level control, not pixel-level.

AEGIS browses in **its own Chromium**, always, unless you say otherwise. That
Chromium is launched by Playwright with a persistent profile inside the AEGIS
folder, shows as a real window you can grab the mouse in, and is mirrored into
the Browser tab so you can watch and drive it from inside the harness.

This is a deliberate default, and the reasoning is worth writing down:

  * Your Chrome is *yours*. It holds your mail, your bank, your work
    sessions, and dozens of tabs you care about. An agent loose in it can close
    a tab you needed, act inside a session you never meant to lend it, or leave
    a page half-filled. Nothing should end up there by accident.
  * AEGIS's own Chromium starts signed out of everything. You sign in to what a
    task actually needs, once, and it sticks in that profile. The blast radius
    is one folder you can delete.

So there is no automatic fallback into Chrome, in either direction. Asking for
Chrome and finding it unreachable is an error that says so, not a quiet
substitution - a silent swap either way is how an agent ends up somewhere you
did not send it.

Three ways to browse, and they are not interchangeable:

  1. **AEGIS Chromium** (the default, everything here). Page-level control:
     open, read, click, type.
  2. **Your real Chrome**, over the DevTools protocol on 127.0.0.1:9222 - only
     when you ask for it by name, and only if you started Chrome with
     `--remote-debugging-port=9222`. It inherits the sessions you are already
     signed into, which is both the point and the risk.
  3. **Computer use** (a separate toolset entirely) - the actual mouse and
     keyboard on your desktop, for driving apps that are not a web page. Not a
     browser tool and never used for browsing unless you say so.

Elements are addressed by **reference number, not CSS selector.** Reading a page
tags every interactive element with a ref and returns a numbered list; clicking
takes that number. Models are poor at inventing selectors and excellent at
picking from a list, and a stale ref fails loudly instead of clicking the wrong
thing.

Navigation is classified as a read. That is a judgement call: the vast majority
of navigations are reading a page, and asking permission for every URL would
make browsing unusable. It does mean a link whose GET has a side effect could be
followed without a prompt. Clicking and typing are writes and always follow your
policy.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from ..config import DATA_DIR, settings
from .base import READ, WRITE, Tool

CDP_URL = "http://127.0.0.1:9222"
PROFILE_DIR = DATA_DIR / "browser-profile"
MAX_TEXT = 12000
MAX_ELEMENTS = 120

AEGIS = "aegis"          # our own Chromium - the default
CHROME = "chrome"        # your real Chrome, over CDP, only when asked for
TARGETS = (AEGIS, CHROME)


def target() -> str:
    """Which browser AEGIS uses unless told otherwise.

    Migrates the old browser_prefer_chrome flag once: it defaulted to True,
    which is the behaviour this setting exists to reverse, so an upgrade must
    not quietly leave an agent pointed at your Chrome.
    """
    chosen = str(settings.get("browser_target") or "").strip().lower()
    if chosen in TARGETS:
        return chosen
    settings.set("browser_target", AEGIS)
    if settings.get("browser_prefer_chrome"):
        settings.set("browser_prefer_chrome", False)
    return AEGIS

_session: "BrowserSession | None" = None
_lock = asyncio.Lock()


# JS that tags interactive elements and reports them. Runs in the page, so it
# must be self-contained and must not throw on odd documents.
_SNAPSHOT_JS = """
(max) => {
  const out = [];
  const sel = 'a[href], button, input, textarea, select, [role=button],' +
              '[role=link], [role=textbox], [role=checkbox], [contenteditable=true]';
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (n >= max) break;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    n += 1;
    el.setAttribute('data-aegis-ref', String(n));
    const tag = el.tagName.toLowerCase();
    let label = (el.getAttribute('aria-label') || el.innerText ||
                 el.value || el.getAttribute('placeholder') ||
                 el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
    if (label.length > 90) label = label.slice(0, 90) + '…';
    let kind = tag;
    if (tag === 'input') kind = 'input:' + (el.type || 'text');
    out.push({ ref: n, kind, label, href: el.getAttribute('href') || '' });
  }
  return out;
}
"""


def available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("Playwright is not installed. Run: pip install playwright "
                       "&& python -m playwright install chromium")


def enabled() -> bool:
    return bool(settings.get("browser_use_enabled", True))


class BrowserSession:
    def __init__(self) -> None:
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.mode: str = ""            # "chrome-cdp" | "own-chromium"
        self.last_refs: list[dict[str, Any]] = []

    @property
    def live(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    async def start(self, want: str = "") -> dict[str, Any]:
        """Start the browser you asked for. No substitutions.

        `want` names a target explicitly; leaving it blank uses the configured
        one, which is AEGIS's own Chromium unless you have changed it. If the
        session is already live on a different target it is closed first, so
        asking for Chrome mid-task actually moves you there rather than
        silently carrying on where you were.
        """
        want = (want or "").strip().lower() or target()
        if want not in TARGETS:
            return {"ok": False, "error": f"Unknown browser {want!r}."}

        if self.live:
            if self.mode == want:
                return {"ok": True, "mode": self.mode, "reused": True}
            await self.close()

        ok, hint = available()
        if not ok:
            return {"ok": False, "error": hint}

        from playwright.async_api import async_playwright

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if want == CHROME:
            cdp = settings.get("browser_cdp_url") or CDP_URL
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(
                    cdp, timeout=4000)
            except Exception as exc:
                # Deliberately not falling back to our own Chromium. You asked
                # for the browser holding your signed-in sessions; quietly
                # using a different one would look like it worked.
                return {"ok": False, "target": CHROME,
                        "error": f"Could not reach your Chrome on {cdp} "
                                 f"({type(exc).__name__}). Chrome has to be "
                                 f"started with --remote-debugging-port=9222 "
                                 f"for anything to attach to it. AEGIS has not "
                                 f"browsed anywhere instead - say the word and "
                                 f"it will use its own Chromium."}
            contexts = self.browser.contexts
            self.context = (contexts[0] if contexts
                            else await self.browser.new_context())
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            self.mode = CHROME
            return {"ok": True, "mode": self.mode, "cdp": cdp}

        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=bool(settings.get("browser_headless", False)),
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"])
            self.browser = None
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            self.mode = AEGIS
            return {"ok": True, "mode": self.mode, "profile": str(PROFILE_DIR)}
        except Exception as exc:
            return {"ok": False, "target": AEGIS,
                    "error": f"Could not start AEGIS's browser: "
                             f"{type(exc).__name__}: {exc}. If Chromium is "
                             f"missing, run: python -m playwright install chromium"}

    async def close(self) -> None:
        try:
            if self.context and self.mode == AEGIS:
                await self.context.close()
            if self.browser and self.mode == CHROME:
                await self.browser.close()      # detaches; does not kill Chrome
        except Exception:
            pass
        finally:
            self.page = self.context = self.browser = None
            self.mode = ""
            self.last_refs = []

    async def shutdown(self) -> None:
        await self.close()
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.playwright = None


async def session() -> BrowserSession:
    global _session
    async with _lock:
        if _session is None:
            _session = BrowserSession()
        return _session


async def _ready(want: str = "") -> tuple[BrowserSession | None,
                                          dict[str, Any] | None]:
    sess = await session()
    if not sess.live or (want and sess.mode != want):
        result = await sess.start(want)
        if not result.get("ok"):
            return None, {"text": result.get("error", "No browser."), "error": True}
    return sess, None


async def _snapshot(sess: BrowserSession) -> list[dict[str, Any]]:
    try:
        refs = await sess.page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)
    except Exception:
        refs = []
    sess.last_refs = refs or []
    return sess.last_refs


def _format_refs(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "(no interactive elements found)"
    lines = []
    for r in refs:
        target = f" → {r['href'][:60]}" if r.get("href") else ""
        lines.append(f"  [{r['ref']}] {r['kind']}: {r['label'] or '(no label)'}{target}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def _open(url: str = "", **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    url = (url or "").strip()
    if not url:
        return {"text": "A url is required.", "error": True}
    if not url.startswith(("http://", "https://", "file://", "about:")):
        url = "https://" + url
    try:
        await sess.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await sess.page.wait_for_timeout(600)
    except Exception as exc:
        return {"text": f"Could not open {url}: {type(exc).__name__}: {exc}",
                "error": True}
    return await _read()


async def _read(max_chars: int = MAX_TEXT, **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        title = await sess.page.title()
        url = sess.page.url
        text = await sess.page.inner_text("body", timeout=15000)
    except Exception as exc:
        return {"text": f"Could not read the page: {type(exc).__name__}: {exc}",
                "error": True}

    refs = await _snapshot(sess)
    text = " \n".join(line.strip() for line in text.splitlines() if line.strip())
    limit = int(max_chars or MAX_TEXT)
    clipped = text[:limit] + ("\n…[truncated]" if len(text) > limit else "")

    return {"text": f"# {title}\n{url}\n\n{clipped}\n\n"
                    f"Interactive elements ({len(refs)}):\n{_format_refs(refs)}"}


async def _find(query: str = "", **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    refs = await _snapshot(sess)
    needle = (query or "").lower().strip()
    hits = [r for r in refs
            if needle in (r.get("label", "") + " " + r.get("href", "")).lower()]
    if not hits:
        return {"text": f"Nothing matching {query!r} among "
                        f"{len(refs)} interactive elements."}
    return {"text": f"Matches for {query!r}:\n{_format_refs(hits)}"}


async def _click(ref: int = 0, **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return {"text": "ref must be the number from the element list.", "error": True}
    if not sess.last_refs:
        return {"text": "Read the page first so elements have reference numbers.",
                "error": True}
    known = {r["ref"] for r in sess.last_refs}
    if ref not in known:
        return {"text": f"No element [{ref}] on this page. Read it again - the "
                        f"page may have changed.", "error": True}
    try:
        await sess.page.click(f'[data-aegis-ref="{ref}"]', timeout=15000)
        await sess.page.wait_for_timeout(900)
    except Exception as exc:
        return {"text": f"Click on [{ref}] failed: {type(exc).__name__}: {exc}",
                "error": True}
    label = next((r["label"] for r in sess.last_refs if r["ref"] == ref), "")
    result = await _read()
    result["text"] = f"Clicked [{ref}] {label!r}.\n\n{result['text']}"
    return result


async def _type(ref: int = 0, text: str = "", submit: bool = False,
                **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        selector = f'[data-aegis-ref="{int(ref)}"]'
        await sess.page.fill(selector, text, timeout=15000)
        if submit:
            await sess.page.press(selector, "Enter")
            await sess.page.wait_for_timeout(1200)
    except Exception as exc:
        return {"text": f"Typing into [{ref}] failed: {type(exc).__name__}: {exc}",
                "error": True}
    if submit:
        result = await _read()
        result["text"] = f"Typed into [{ref}] and pressed Enter.\n\n{result['text']}"
        return result
    return {"text": f"Typed {len(text)} characters into [{ref}]."}


async def _back(**_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        await sess.page.go_back(wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return {"text": f"Could not go back: {exc}", "error": True}
    return await _read()


async def _forward(**_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        await sess.page.go_forward(wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return {"text": f"Could not go forward: {exc}", "error": True}
    return await _read()


async def _reload(**_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        await sess.page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        return {"text": f"Could not reload: {exc}", "error": True}
    return await _read()


async def _use_chrome(confirm: bool = False, **_: Any) -> dict[str, Any]:
    """Move this session into the user's real Chrome. Only on an explicit ask.

    A write, and gated on confirm, because it is the one action here that
    reaches outside AEGIS's own sandbox and into the browser holding the user's
    live sessions. It should never happen as a workaround for something else
    failing.
    """
    if not confirm:
        return {"text": "This switches browsing into your own Chrome, with "
                        "every session you are signed into. Call it again with "
                        "confirm=true only if the user asked for their own "
                        "Chrome by name.", "error": True}
    sess = await session()
    result = await sess.start(CHROME)
    if not result.get("ok"):
        return {"text": result.get("error", "Could not attach to Chrome."),
                "error": True}
    return {"text": f"Now browsing in your own Chrome ({result.get('cdp')}). "
                    f"This lasts until the session is closed or switched back."}


async def _use_aegis(**_: Any) -> dict[str, Any]:
    sess = await session()
    result = await sess.start(AEGIS)
    if not result.get("ok"):
        return {"text": result.get("error", "Could not start the browser."),
                "error": True}
    return {"text": "Back in AEGIS's own browser, separate from your Chrome."}


async def _screenshot(**_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        raw = await sess.page.screenshot(type="jpeg", quality=70)
    except Exception as exc:
        return {"text": f"Screenshot failed: {exc}", "error": True}
    return {"text": f"Screenshot of {sess.page.url}",
            "images": [("image/jpeg", base64.b64encode(raw).decode())]}


async def _tabs(**_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        pages = sess.context.pages
        lines = []
        for i, page in enumerate(pages):
            mark = " (current)" if page is sess.page else ""
            lines.append(f"  [{i}] {await page.title()} — {page.url}{mark}")
        return {"text": f"{len(pages)} tab(s):\n" + "\n".join(lines)}
    except Exception as exc:
        return {"text": f"Could not list tabs: {exc}", "error": True}


async def _switch_tab(index: int = 0, **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    pages = sess.context.pages
    index = int(index)
    if not 0 <= index < len(pages):
        return {"text": f"No tab [{index}]. There are {len(pages)}.", "error": True}
    sess.page = pages[index]
    await sess.page.bring_to_front()
    return await _read()


async def _new_tab(url: str = "", **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    sess.page = await sess.context.new_page()
    if url:
        return await _open(url)
    return {"text": "Opened a new blank tab."}


MODE_LABEL = {AEGIS: "AEGIS's own Chromium", CHROME: "your Chrome"}


# ---------------------------------------------------------------------------
# What the Browser tab in the UI needs
# ---------------------------------------------------------------------------

async def page_state(want: str = "", max_chars: int = 4000) -> dict[str, Any]:
    """Structured page state for the UI, rather than the text blob the model
    gets. Same session and same element numbering, so a ref you click in the
    harness is the same ref the agent would click."""
    sess, err = await _ready(want)
    if err:
        return {"ok": False, "error": err["text"]}
    try:
        title = await sess.page.title()
        url = sess.page.url
        text = await sess.page.inner_text("body", timeout=15000)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    refs = await _snapshot(sess)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return {"ok": True, "url": url, "title": title, "mode": sess.mode,
            "mode_label": MODE_LABEL.get(sess.mode, sess.mode),
            "elements": refs,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars}


async def view_jpeg(width: int = 0) -> bytes | None:
    """A frame of the live page for the Browser tab. Returns None rather than
    raising, because the view polls and a blank frame beats a broken tab."""
    sess = await session()
    if not sess.live:
        return None
    try:
        return await sess.page.screenshot(type="jpeg", quality=62)
    except Exception:
        return None


async def status() -> dict[str, Any]:
    ok, hint = available()
    sess = await session()
    title = ""
    if sess.live:
        try:
            title = await sess.page.title()
        except Exception:
            title = ""
    return {
        "enabled": enabled(),
        "available": ok,
        "hint": hint,
        "target": target(),
        "mode": sess.mode or "",
        "mode_label": MODE_LABEL.get(sess.mode, "not started"),
        "live": sess.live,
        "url": sess.page.url if sess.live else "",
        "title": title,
        "headless": bool(settings.get("browser_headless", False)),
        "cdp_url": settings.get("browser_cdp_url") or CDP_URL,
        "profile_dir": str(PROFILE_DIR),
        "tool_count": len(tools()),
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# v2.1: you can take the browser over from the docked panel. While you hold
# it, the agent may still read pages but every action waits for you.
USER_CONTROL = {"on": False, "since": 0.0}
_ACTIONS = {"_click", "_type", "_back", "_forward", "_reload", "_switch_tab",
            "_new_tab", "_scroll", "_press", "_open"}


def set_user_control(on: bool) -> dict[str, Any]:
    import time
    USER_CONTROL["on"] = bool(on)
    USER_CONTROL["since"] = time.time() if on else 0.0
    return dict(USER_CONTROL)


async def _scroll(direction: str = "down", amount: int = 700, **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    dy = -abs(int(amount or 700)) if str(direction).lower() == "up" else abs(int(amount or 700))
    try:
        await sess.page.mouse.wheel(0, dy)
        await sess.page.wait_for_timeout(500)
    except Exception as exc:
        return {"text": f"Scroll failed: {exc}", "error": True}
    result = await _read()
    result["text"] = f"Scrolled {direction}.\n\n{result['text']}"
    return result


async def _press(key: str = "Enter", **_: Any) -> dict[str, Any]:
    sess, err = await _ready()
    if err:
        return err
    try:
        await sess.page.keyboard.press(str(key or "Enter"))
        await sess.page.wait_for_timeout(700)
    except Exception as exc:
        return {"text": f"Key press failed: {exc}", "error": True}
    return {"text": f"Pressed {key}."}


def _wrap(fn):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if USER_CONTROL["on"] and fn.__name__ in _ACTIONS:
            return {"text": "The user has taken control of the browser in the AEGIS "
                            "panel. Do not act on the page now: tell him what you "
                            "need done, or wait until he hands control back.",
                    "error": True}
        try:
            return await fn(**args)
        except Exception as exc:
            return {"text": f"{type(exc).__name__}: {exc}", "error": True}
    return handler


def tools() -> list[Tool]:
    if not enabled():
        return []
    ok, _ = available()
    if not ok:
        return []

    def tool(name, desc, schema, risk, fn) -> Tool:
        return Tool(name=f"browser__{name}", raw_name=name, description=desc,
                    input_schema=schema, server="browser", origin="builtin",
                    risk=risk, handler=_wrap(fn))

    ref_field = {"ref": {"type": "integer",
                         "description": "The [number] from the element list"}}

    return [
        tool("open",
             "Open a URL in AEGIS's own browser and return the page text plus a "
             "numbered list of every link, button and field on it. This is the "
             "way to browse: use it for anything on the web. It runs in AEGIS's "
             "own Chromium, separate from the user's Chrome, so it is safe to "
             "use without asking. Do NOT reach for computer use or the user's "
             "own Chrome to visit a web page - only if they asked for those by "
             "name. Start here.",
             {"type": "object", "properties": {"url": {"type": "string"}},
              "required": ["url"]},
             READ, _open),

        tool("read",
             "Re-read the current page. Do this after anything that changes it, "
             "because element numbers are reassigned each read.",
             {"type": "object",
              "properties": {"max_chars": {"type": "integer"}}},
             READ, _read),

        tool("find",
             "Search the current page's interactive elements by their label or "
             "link target. Cheaper than re-reading a long page.",
             {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"]},
             READ, _find),

        tool("click", "Click an element by its reference number.",
             {"type": "object", "properties": ref_field, "required": ["ref"]},
             WRITE, _click),

        tool("type",
             "Type into a field by its reference number. Set submit to press "
             "Enter afterwards.",
             {"type": "object",
              "properties": {**ref_field, "text": {"type": "string"},
                             "submit": {"type": "boolean"}},
              "required": ["ref", "text"]},
             WRITE, _type),

        tool("scroll", "Scroll the page up or down and re-read it.",
             {"type": "object",
              "properties": {"direction": {"type": "string", "enum": ["down", "up"]},
                             "amount": {"type": "integer"}}},
             READ, _scroll),

        tool("press", "Press a keyboard key on the page (Enter, Escape, Tab, "
             "ArrowDown, PageDown…).",
             {"type": "object", "properties": {"key": {"type": "string"}},
              "required": ["key"]},
             WRITE, _press),

        tool("back", "Go back one page in history.",
             {"type": "object", "properties": {}}, WRITE, _back),

        tool("forward", "Go forward one page in history.",
             {"type": "object", "properties": {}}, WRITE, _forward),

        tool("reload", "Reload the current page.",
             {"type": "object", "properties": {}}, WRITE, _reload),

        tool("screenshot",
             "Picture of the current page. Use it when the layout matters or "
             "the text extraction is not enough.",
             {"type": "object", "properties": {}}, READ, _screenshot),

        tool("tabs", "List the open tabs.",
             {"type": "object", "properties": {}}, READ, _tabs),

        tool("switch_tab", "Make another tab the active one.",
             {"type": "object",
              "properties": {"index": {"type": "integer"}}, "required": ["index"]},
             WRITE, _switch_tab),

        tool("new_tab", "Open a new tab, optionally at a URL.",
             {"type": "object", "properties": {"url": {"type": "string"}}},
             WRITE, _new_tab),

        tool("use_my_chrome",
             "Switch browsing into the user's OWN Chrome, inheriting every "
             "site they are signed into. Use this ONLY when the user has asked "
             "for their own Chrome by name in this conversation. Never use it "
             "because a page needed a login, because something failed, or to "
             "save them a sign-in - AEGIS's own browser keeps its own logins "
             "and is the right place for those. Requires Chrome to have been "
             "started with --remote-debugging-port=9222.",
             {"type": "object",
              "properties": {"confirm": {
                  "type": "boolean",
                  "description": "true only if the user asked for their own "
                                 "Chrome by name"}},
              "required": ["confirm"]},
             WRITE, _use_chrome),

        tool("use_aegis_browser",
             "Switch back to AEGIS's own browser, away from the user's Chrome.",
             {"type": "object", "properties": {}}, WRITE, _use_aegis),
    ]
