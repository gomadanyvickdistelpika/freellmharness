"""AEGIS v2.2: Needle, the offline fast-intent step.

Runs without Needle installed: the engine is replaced by a scripted fake that
returns the same envelope shape cactus-needle 3.x returns
({"type": "call", "function_calls": [...], "confidence": 0.9, "validation": {}}).
The last section runs the real engine only if it is installed and cached."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


def call(name, args=None, confidence=0.9, **extra):
    return {"type": "call", "confidence": confidence,
            "function_calls": [{"name": name, "arguments": args or {}}], **extra}


class FakeNeedle:
    """Answers from a dict of text -> envelope; counts calls."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete(self, text, max_new_tokens=512):
        self.calls.append(text)
        answer = self.answers.get(text, {"type": "text", "function_calls": [],
                                         "confidence": 0.1})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


def _use(fake):
    from aegis import needle_intent
    needle_intent.engine.reset()
    needle_intent.factory = lambda: fake


def _gates() -> None:
    from aegis import needle_intent as ni
    from aegis.config import settings

    settings.set("needle_enabled", True)
    check("plain short text is allowed through the pre-gate", ni.pregate("open youtube") == "")
    check("slash commands are left alone", ni.pregate("/new") != "")
    check("multi-line text is never a command", ni.pregate("a\nb") != "")
    check("long text is never a command", ni.pregate("x " * 200) != "")
    check("code is never a command", ni.pregate("import os; print(1)") != "")
    settings.set("needle_enabled", False)
    check("switched off means nothing is asked", ni.pregate("open youtube") != "")
    settings.set("needle_enabled", True)

    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube.com"}))
    check("a confident browser command becomes /browser",
          r["matched"] and r["slash"] == "/browser https://youtube.com", str(r))
    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube"}))
    check("a bare site name gets .com", r.get("slash") == "/browser https://youtube.com", str(r))
    r = ni.interpret("open youtube", call("open_browser", {"url": "facebook.com"}))
    check("an invented web address is refused", not r["matched"], r["reason"])
    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube.com"}, 0.3))
    check("low confidence is refused", not r["matched"] and "confidence" in r["reason"])
    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube.com"}, None))
    check("a missing confidence score is refused", not r["matched"])
    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube.com"},
                                          validation={"ungrounded": ["open_browser.url"]}))
    check("Needle's own ungrounded flag is respected", not r["matched"])
    r = ni.interpret("don't open youtube", call("open_browser", {"url": "youtube.com"},
                                                validation={"negation": True}))
    check("a negated request is refused", not r["matched"])
    r = ni.interpret("undo that", call("undo_last_change"))
    check("tools AEGIS never exposed are refused", not r["matched"] and "unknown" in r["reason"])
    two = call("new_chat")
    two["function_calls"].append({"name": "show_files", "arguments": {}})
    check("two actions at once go to the model", not ni.interpret("new chat and files", two)["matched"])
    r = ni.interpret("boost off", call("set_boost", {"mode": "off"}))
    check("boost off becomes /boost off", r.get("slash") == "/boost off", str(r))
    r = ni.interpret("boost max", call("set_boost", {"mode": "max"}))
    check("a boost mode outside the list is refused", not r["matched"])
    r = ni.interpret("switch to the job hunt project",
                     call("switch_project", {"name": "job hunt"}))
    check("project switch keeps your words", r.get("slash") == "/project job hunt", str(r))
    r = ni.interpret("switch to the job hunt project",
                     call("switch_project", {"name": "vmware lab"}))
    check("a project name you didn't say is refused", not r["matched"])
    r = ni.interpret("make an image of a lighthouse at dusk",
                     call("make_image", {"prompt": "a lighthouse\nat dusk"}))
    check("free-text arguments are flattened to one line",
          r.get("slash") == "/image a lighthouse at dusk", str(r))
    r = ni.interpret("make an image", call("make_image", {}))
    check("a missing required argument is refused", not r["matched"])
    check("a text answer is not a command",
          not ni.interpret("why", {"type": "text", "function_calls": []})["matched"])
    check("/undo and /computer are never offered to Needle",
          "undo" not in " ".join(ni.COMMANDS) and "computer" not in " ".join(ni.COMMANDS))
    r = ni.interpret("write me a cover letter for a VMware support job",
                     call("make_video", {"description": "a cover letter"}, 0.97))
    check("the real false positive (cover letter -> make_video) is now refused",
          not r["matched"] and "words" in r["reason"], r["reason"])
    r = ni.interpret("make a short video of a city at night",
                     call("make_video", {"description": "a city at night"}, 0.9))
    check("...while a real video request still works", r["matched"], r["reason"])
    r = ni.interpret("switch to the coder agent",
                     call("switch_agent", {"name": "coder agent"}, 1.0))
    check("'coder agent' becomes /agent coder, with nothing left over to send",
          r.get("slash") == "/agent coder", str(r.get("slash")))
    r = ni.interpret("compare OpenRouter and xKiro free models",
                     call("open_browser", {"url": "OpenRouter"}, 0.95))
    check("'open' inside a word (OpenRouter) is not the word open", not r["matched"],
          r["reason"])
    r = ni.interpret("summarise this week's job search",
                     call("web_search", {"query": "this week's job"}, 0.95))
    check("'job search' is not a web search", not r["matched"], r["reason"])
    settings.set("needle_min_confidence", 0.95)
    r = ni.interpret("open youtube", call("open_browser", {"url": "youtube.com"}, 0.9))
    check("the confidence threshold comes from settings", not r["matched"])
    settings.set("needle_min_confidence", 0.6)


async def _detect() -> None:
    from aegis import needle_intent as ni

    fake = FakeNeedle({
        "open youtube": call("open_browser", {"url": "youtube.com"}, 0.92),
        "new chat please": call("new_chat", {}, 0.88),
        "crash": RuntimeError("engine fell over"),
    })
    _use(fake)
    r = await ni.detect("open youtube")
    check("detect runs the engine and returns the slash command",
          r["matched"] and r["slash"] == "/browser https://youtube.com", str(r))
    check("detect reports how long the engine took", isinstance(r.get("ms"), float))
    r = await ni.detect("write me a cover letter for a VMware job")
    check("a real request is not hijacked", not r["matched"], r["reason"])
    r = await ni.detect("crash")
    check("an engine error is 'not matched', never an exception",
          not r["matched"] and "error" in r["reason"])
    check("the error is visible in status", "fell over" in ni.status()["error"])
    before = len(fake.calls)
    await ni.detect("/new")
    await ni.detect("line one\nline two")
    check("pre-gated text never reaches the engine", len(fake.calls) == before)
    # A failed load is not retried on every message.
    tries = []

    def broken():
        tries.append(1)
        raise OSError("no engine DLL")
    ni.engine.reset()
    ni.factory = broken
    a = await ni.detect("open youtube")
    b = await ni.detect("open youtube")
    check("a failed load backs off instead of retrying each message",
          not a["matched"] and not b["matched"] and len(tries) == 1, str(len(tries)))
    w = await ni.warmup()
    check("...but Prepare Needle always retries", not w["ok"] and len(tries) == 2)
    _use(fake)
    results = await asyncio.gather(*[ni.detect("new chat please") for _ in range(8)])
    check("concurrent requests are serialised safely",
          all(x["slash"] == "/new" for x in results))


def _endpoints() -> None:
    from fastapi.testclient import TestClient

    from aegis import needle_intent as ni
    from aegis.server import app

    _use(FakeNeedle({"turn boost off": call("set_boost", {"mode": "off"}, 0.9)}))
    with TestClient(app) as client:
        r = client.post("/api/intent", json={"text": "turn boost off"})
        check("POST /api/intent matches a command",
              r.status_code == 200 and r.json().get("slash") == "/boost off", r.text[:200])
        r = client.post("/api/intent", json={"text": "tell me a joke about vSAN"})
        check("POST /api/intent leaves other text alone",
              r.status_code == 200 and r.json()["matched"] is False)
        r = client.post("/api/intent", json={})
        check("an empty body is handled", r.status_code == 200 and not r.json()["matched"])
        r = client.get("/api/intent/status")
        check("status lists the commands", "open_browser" in r.json().get("commands", []))
        r = client.post("/api/intent/warmup")
        check("warmup loads the engine", r.status_code == 200 and r.json()["loaded"], r.text[:200])
        r = client.post("/api/settings", json={"needle_min_confidence": 0.75,
                                                "needle_enabled": False})
        s = r.json()["settings"]
        check("Needle settings can be saved",
              s["needle_min_confidence"] == 0.75 and s["needle_enabled"] is False)
        r = client.post("/api/intent", json={"text": "turn boost off"})
        check("switched off in settings means no match", not r.json()["matched"])
        client.post("/api/settings", json={"needle_min_confidence": 0.6,
                                           "needle_enabled": True})
        page = client.get("/").text
        check("the UI loads needle.js", "/static/needle.js" in page)
        check("needle.js is served", client.get("/static/needle.js").status_code == 200)
    # Not installed: the real factory must say so rather than crash.
    ni.engine.reset()
    ni.factory = ni._default_factory
    if not ni.installed():
        r = asyncio.run(ni.detect("open youtube"))
        check("without cactus-needle the step is skipped with a hint",
              not r["matched"] and "pip install" in r["reason"])
        r = asyncio.run(ni.warmup())
        check("warmup without cactus-needle explains how to install", not r["ok"]
              and "cactus-needle" in r["error"])


def _real_engine() -> None:
    from aegis import needle_intent as ni

    ni.engine.reset()
    ni.factory = ni._default_factory
    if not ni.installed():
        print("  (cactus-needle not installed - skipping the real-engine check)")
        return
    try:
        asyncio.run(ni.warmup())
    except Exception:
        pass
    if ni.engine.agent is None:
        print(f"  (Needle engine not available here: {ni.engine.error[:120]} - skipped)")
        return
    commands = [
        ("open youtube.com", "/browser"), ("turn boost off", "/boost"),
        ("start a new chat", "/new"), ("make an image of a lighthouse at dusk", "/image"),
        ("search the web for SOC analyst jobs in London", "/search"),
        ("show my files", "/files"), ("switch to the coder agent", "/agent coder"),
        ("turn hands-free mode on", "/talk"),
    ]
    # Everyday requests that must reach the model untouched. Kept separate from
    # anything the gates were tuned on, so it measures rather than confirms.
    requests = [
        "write me a cover letter for a VMware support job",
        "why does my vCenter certificate expire?",
        "summarise this week's job search",
        "explain what vSAN witness does",
        "help my son with his maths homework",
        "draft a follow-up email to the recruiter",
        "what's a good knee-friendly workout",
        "translate bonjour into Spanish",
        "compare OpenRouter and xKiro free models",
        "is my old car worth fixing",
        "tell me a joke",
        "how do I renew my residence permit",
    ]
    hits = 0
    for text, want in commands:
        r = asyncio.run(ni.detect(text))
        ok = (r.get("slash") or "").startswith(want)
        hits += ok
        print(f"  command  {'✓' if ok else '·'} {text!r} -> {r.get('slash') or r.get('reason')} "
              f"[{r.get('tool')} conf={r.get('confidence')} {r.get('ms')}ms]")
    grabbed = []
    for text in requests:
        r = asyncio.run(ni.detect(text))
        if r["matched"]:
            grabbed.append(text)
        print(f"  request  {'✗ GRABBED' if r['matched'] else '✓ left'} {text!r} "
              f"[{r.get('tool')} conf={r.get('confidence')}: {r.get('reason')}]")
    print(f"  commands recognised: {hits}/{len(commands)}   "
          f"requests wrongly grabbed: {len(grabbed)}/{len(requests)}")
    check("real engine: no everyday request is treated as a command", not grabbed,
          "; ".join(grabbed))
    check("real engine: most commands are recognised", hits >= len(commands) - 2,
          f"{hits}/{len(commands)}")


def run_all() -> None:
    section("Needle gates")
    _gates()
    section("Needle detect")
    asyncio.run(_detect())
    section("Needle API and UI")
    _endpoints()
    section("Needle real engine (optional)")
    _real_engine()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-v22-test-"))
    from harness import report
    run_all()
    sys.exit(report())
