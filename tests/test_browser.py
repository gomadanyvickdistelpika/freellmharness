"""Browsing: in AEGIS's own Chromium, and never anywhere else by accident.

The rule this file defends is a boundary, not a preference. An agent browses in
AEGIS's own browser; it reaches the user's real Chrome only when the user asked
for Chrome by name. The dangerous failure is not a crash - it is a quiet
substitution, where something fails and the agent ends up driving the browser
holding the user's mail and bank sessions without anyone deciding that.

So the tests that matter most here are the negative ones: asking for Chrome and
not getting silently handed AEGIS's browser, and asking for AEGIS's browser and
never touching Chrome at all.

Where Chromium is installed these run against a real browser and a real local
page. Where it is not, the browser-driving section skips itself rather than
reporting a failure the machine cannot avoid.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

PAGE = """<!doctype html><html><head><title>Test page</title></head><body>
<h1>Hello from the test server</h1>
<p>Some body text that should come back when the page is read.</p>
<a href="/second" id="go">Go to the second page</a>
<input type="text" placeholder="search here">
<button>A button</button>
</body></html>"""

SECOND = """<!doctype html><html><head><title>Second</title></head><body>
<h1>You arrived on the second page</h1></body></html>"""


def serve() -> tuple[ThreadingHTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802
            body = (SECOND if self.path.startswith("/second") else PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def fresh() -> None:
    from aegis.config import settings
    settings.update({"browser_target": "aegis", "browser_prefer_chrome": False,
                     "browser_use_enabled": True, "browser_headless": True,
                     "browser_cdp_url": ""})


# ---------------------------------------------------------------------------
# 1. Which browser, and how that is decided
# ---------------------------------------------------------------------------

def _target_choice() -> None:
    from aegis.config import settings
    from aegis.tools import browser

    settings.update({"browser_target": "", "browser_prefer_chrome": False})
    check("with nothing configured, agents get AEGIS's own browser",
          browser.target() == browser.AEGIS, browser.target())

    # The upgrade path that would otherwise be a silent regression: the old
    # flag defaulted to preferring Chrome, which is the behaviour this whole
    # change reverses.
    settings.update({"browser_target": "", "browser_prefer_chrome": True})
    check("an old install that preferred Chrome is migrated to AEGIS's browser",
          browser.target() == browser.AEGIS, browser.target())
    check("...and the old flag is turned off so it cannot come back",
          settings.get("browser_prefer_chrome") is False)

    settings.set("browser_target", "chrome")
    check("choosing Chrome deliberately is respected",
          browser.target() == browser.CHROME)

    settings.set("browser_target", "nonsense")
    check("an unrecognised setting falls back to AEGIS, not to Chrome",
          browser.target() == browser.AEGIS, browser.target())
    fresh()


async def _no_silent_chrome() -> None:
    """Asking for Chrome when Chrome is not there must fail, not substitute."""
    from aegis.config import settings
    from aegis.tools import browser

    fresh()
    # A port with nothing on it. Chrome is emphatically not listening.
    settings.set("browser_cdp_url", "http://127.0.0.1:9")

    sess = browser.BrowserSession()
    try:
        result = await sess.start(browser.CHROME)
        check("attaching to a Chrome that is not listening fails",
              result.get("ok") is False, str(result)[:120])
        check("...and it does not quietly start AEGIS's browser instead",
              sess.mode == "" and sess.page is None,
              f"mode={sess.mode!r} page={sess.page!r}")
        check("...and says plainly that nothing was browsed",
              "has not browsed anywhere instead" in result.get("error", ""),
              result.get("error", "")[:160])
        check("...and says how to make Chrome reachable",
              "remote-debugging-port=9222" in result.get("error", ""),
              result.get("error", "")[:160])
    finally:
        await sess.shutdown()
    fresh()


async def _tool_gate() -> None:
    """The switch into the user's Chrome is a deliberate act, not a fallback."""
    from aegis.tools import browser

    fresh()
    names = {t.raw_name for t in browser.tools()}
    check("the browser toolset is offered", "open" in names and "read" in names)
    check("history controls are there for the UI and the agent alike",
          {"back", "forward", "reload"} <= names, str(sorted(names)))
    check("switching to the user's Chrome is a tool of its own",
          "use_my_chrome" in names)
    check("...and so is switching back",
          "use_aegis_browser" in names)

    switch = next(t for t in browser.tools() if t.raw_name == "use_my_chrome")
    check("switching to Chrome counts as a write, so the policy gates it",
          switch.risk == "write", switch.risk)
    check("...and its description says when NOT to use it",
          "Never use it because" in switch.description, switch.description[:80])

    refused = await switch.handler({"confirm": False})
    check("it refuses without an explicit confirmation",
          refused.get("error") is True, str(refused)[:120])
    check("...and explains what confirming would mean",
          "your own Chrome" in refused.get("text", ""),
          refused.get("text", "")[:120])

    opener = next(t for t in browser.tools() if t.raw_name == "open")
    check("the ordinary browse tool points agents away from computer use",
          "computer use" in opener.description.lower(),
          opener.description[:90])


def _computer_use_stays_out_of_browsing() -> None:
    from aegis.config import settings
    from aegis.tools import computer

    settings.set("computer_use_enabled", True)

    # Checked on the module itself, not on the tool list: computer use is
    # unavailable on a machine with no display, and the boundary still has to
    # be stated there - that is where a reader and a model both look.
    doc = computer.__doc__ or ""
    check("computer use states up front that it is not for browsing",
          "Not for browsing" in doc, doc[:60])
    check("...and says what it IS for",
          "native app" in doc or "desktop itself" in doc, doc[:200])

    shot = next((t for t in computer.tools() if t.raw_name == "screenshot"), None)
    if shot is None:
        cap = computer.capability()
        print(f"  (tool descriptions not checked: {cap.detail})")
        return
    check("the desktop screenshot sends web pages to the browser instead",
          "browser tools" in shot.description, shot.description[:100])
    check("...and is explicit that it is the user's own desktop",
          "DESKTOP" in shot.description, shot.description[:60])


# ---------------------------------------------------------------------------
# 2. Actually browsing, in a real Chromium
# ---------------------------------------------------------------------------

async def _real_browsing() -> bool:
    """Drive a real page. Returns False if Chromium is not installed here."""
    from aegis.config import settings
    from aegis.tools import browser

    fresh()
    settings.set("browser_headless", True)

    sess = browser.BrowserSession()
    started = await sess.start()
    if not started.get("ok"):
        print(f"  (skipped: {started.get('error', '')[:90]})")
        await sess.shutdown()
        return False

    check("AEGIS's own browser starts", started.get("ok") is True)
    check("...in its own mode, not Chrome's", sess.mode == browser.AEGIS,
          sess.mode)
    check("...with a profile inside the AEGIS folder, not Chrome's",
          "browser-profile" in started.get("profile", ""),
          started.get("profile", ""))

    server, port = serve()
    # The module-level session is what the tools and the UI share, so point it
    # at this one rather than opening a second browser.
    browser._session = sess
    try:
        state = await browser.page_state()
        result = await browser._open(f"http://127.0.0.1:{port}/")
        check("a page opens", "Test page" in result.get("text", ""),
              result.get("text", "")[:80])
        check("...and its body text comes back",
              "Some body text" in result.get("text", ""))

        state = await browser.page_state()
        check("the UI gets the same page as the agent",
              state["ok"] and state["title"] == "Test page", str(state)[:120])
        kinds = {e["kind"] for e in state["elements"]}
        check("...and the same numbered elements",
              {"a", "button"} <= kinds and any(k.startswith("input")
                                               for k in kinds), str(kinds))

        link = next(e for e in state["elements"] if e["kind"] == "a")
        clicked = await browser._click(link["ref"])
        check("clicking an element by its number navigates",
              "second page" in clicked.get("text", ""),
              clicked.get("text", "")[:90])

        back = await browser._back()
        check("back returns to the first page",
              "Test page" in back.get("text", ""), back.get("text", "")[:80])

        forward = await browser._forward()
        check("forward goes on again",
              "second page" in forward.get("text", ""),
              forward.get("text", "")[:80])

        frame = await browser.view_jpeg()
        check("the live view produces a real JPEG frame",
              frame is not None and frame[:2] == b"\xff\xd8",
              f"{len(frame) if frame else 0} bytes")

        check("a stale element number is refused rather than guessed at",
              (await browser._click(999)).get("error") is True)
    finally:
        server.shutdown()
        browser._session = None
        await sess.shutdown()
    return True


# ---------------------------------------------------------------------------
# 3. The API the Browser tab uses
# ---------------------------------------------------------------------------

def _api() -> None:
    from fastapi.testclient import TestClient

    from aegis.server import app

    fresh()
    with TestClient(app) as client:
        body = client.get("/api/browser").json()
        check("the browser reports which target it will use",
              body["target"] == "aegis", str(body)[:140])
        check("...and says so in words for the UI",
              "own Chromium" in body["mode_label"]
              or body["mode_label"] == "not started", body["mode_label"])

        r = client.post("/api/browser", json={"target": "chrome"})
        check("the target can be switched deliberately",
              r.json()["target"] == "chrome", r.text[:120])
        r = client.post("/api/browser", json={"target": "edge"})
        check("an unknown target is refused rather than guessed",
              r.status_code == 400, str(r.status_code))
        client.post("/api/browser", json={"target": "aegis"})

        check("the live view says 'nothing to show' when no page is open",
              client.get("/api/browser/view").status_code == 204)

        check("opening without a url is refused",
              client.post("/api/browser/open", json={}).status_code == 400)
        check("an unknown action is refused",
              client.post("/api/browser/act",
                          json={"action": "explode"}).status_code == 400)


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("browser: which browser an agent gets")
    _target_choice()
    section("browser: no silent switch into your Chrome")
    asyncio.run(_no_silent_chrome())
    section("browser: the switch is a deliberate act")
    asyncio.run(_tool_gate())
    section("browser: computer use is not for browsing")
    _computer_use_stays_out_of_browsing()
    section("browser: driving a real page")
    asyncio.run(_real_browsing())
    section("browser: the API behind the Browser tab")
    _api()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR",
                          tempfile.mkdtemp(prefix="aegis-browser-test-"))
    from harness import report
    run_all()
    sys.exit(report())
