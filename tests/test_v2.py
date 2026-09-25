"""AEGIS v2: never-error routing, smart routing, Studio, coding tools, web tools,
projects, the Super Agent and quick setup.

Every behaviour promised in the v2 README section is exercised here against
scripted providers and real local HTTP servers - no network needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


class FakeProvider:
    kind = "api"
    supports_tools = True
    supports_images = True
    blurb = ""
    needs = ""

    def __init__(self, key: str, script: list[dict[str, Any]] | None = None,
                 scripts: list[list[dict[str, Any]]] | None = None,
                 kind: str = "api") -> None:
        self.key = key
        self.label = key
        self.kind = kind
        self.script = script or []
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["m"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts) -> AsyncIterator[dict[str, Any]]:
        self.calls.append({"model": model, "messages": [dict(m) for m in messages],
                           "tools": opts.get("tools")})
        script = self.script
        if self.scripts is not None:
            script = self.scripts.pop(0) if self.scripts else []
        for event in script:
            yield event


def ok_script(text: str) -> list[dict[str, Any]]:
    return [{"type": "delta", "text": text},
            {"type": "usage", "input": 5, "output": 5}, {"type": "done"}]


def err(status: int | None, text: str) -> dict[str, Any]:
    return {"type": "error", "status": status, "text": text}


def patch_registry(providers: dict[str, Any]):
    from aegis import providers as provider_mod
    original = provider_mod.registry
    provider_mod.registry = lambda include_routes=False: dict(providers)
    return original, provider_mod


def reset_health() -> None:
    from aegis import router as R
    from aegis.config import settings
    settings.set("model_health", {})
    settings.set("model_caps", {})
    R.book._data.clear()
    R.book._loaded = True


async def collect(provider, messages, **opts) -> list[dict[str, Any]]:
    return [e async for e in provider.chat(messages, "", **opts)]


def text_of(events) -> str:
    return "".join(e.get("text", "") for e in events if e["type"] == "delta")


# ---------------------------------------------------------------------------
# 1. Classification of model-specific refusals
# ---------------------------------------------------------------------------

def _classify() -> None:
    from aegis import router as R

    cases = [
        (400, "This model does not support tools", R.CAPABILITY, "tools"),
        (404, "No endpoints found that support tool use.", R.CAPABILITY, "tools"),
        (400, "Image input is not supported for this model", R.CAPABILITY, "vision"),
        (400, "This model's maximum context length is 8192 tokens", R.CAPABILITY, "context"),
        (413, "request too large", R.CAPABILITY, "context"),
        (400, "messages: invalid role 'banana'", R.BAD_REQUEST, ""),
        (404, "model not found", R.NOT_FOUND, ""),
    ]
    for status, text, kind, gap in cases:
        check(f"{status} '{text[:40]}' -> {kind}", R.classify(status, text) == kind,
              R.classify(status, text))
        check(f"...gap is '{gap or 'none'}'", R.capability_gap(text) == gap,
              R.capability_gap(text))
    check("capability refusals fail over", R.should_failover(R.CAPABILITY))
    check("empty answers fail over", R.should_failover(R.EMPTY))
    check("a genuinely bad request still stops", not R.should_failover(R.BAD_REQUEST))


# ---------------------------------------------------------------------------
# 2. Never-error routing
# ---------------------------------------------------------------------------

async def _never_error() -> None:
    from aegis import router as R
    from aegis.providers.routed import RoutedProvider

    class T:          # a stand-in tool
        name = "files__read"
        def openai_schema(self): return {}

    # --- a model that cannot take tools: next model, and remembered --------
    reset_health()
    a = FakeProvider("a", [err(400, "tools is not supported for this model")])
    b = FakeProvider("b", ok_script("answer from b"))
    orig, mod = patch_registry({"a": a, "b": b})
    try:
        route = R.Route(key="r", label="R", smart=False, candidates=[
            R.Candidate("a", "small"), R.Candidate("b", "big")])
        ev = await collect(RoutedProvider(route),
                           [{"role": "user", "content": "hi"}], tools=[T()])
        check("a no-tools refusal moves to the next model",
              text_of(ev) == "answer from b", text_of(ev))
        check("...without an error reaching the user",
              not any(e["type"] == "error" for e in ev))
        check("...and the gap is remembered", R.caps("a", "small")["tools"] is False)
        check("...but the model is not rested (it still works without tools)",
              not R.book.get("a", "small").resting)
        plan = R.plan(route, require_tools=True)
        check("next time a tool request skips it up front",
              [c.model for c in plan.usable] == ["big"],
              str([c.model for c in plan.usable]))
        plan2 = R.plan(route)
        check("...while plain chat can still use it",
              [c.model for c in plan2.usable] == ["small", "big"])
    finally:
        mod.registry = orig

    # --- nothing tool-capable left: answer without tools, not an error ----
    reset_health()
    R.learn_caps("a", "small", tools=False)
    a = FakeProvider("a", ok_script("plain answer"))
    orig, mod = patch_registry({"a": a})
    try:
        route = R.Route(key="r", label="R", candidates=[R.Candidate("a", "small")])
        ev = await collect(RoutedProvider(route),
                           [{"role": "user", "content": "hi"}], tools=[T()])
        check("with no tool-capable model, it answers without tools",
              text_of(ev) == "plain answer", str(ev)[:200])
        check("...says so in the trace",
              any("without tools" in e.get("text", "") for e in ev
                  if e["type"] == "notice"))
        check("...and really sent no tools", a.calls[0]["tools"] is None)
    finally:
        mod.registry = orig

    # --- context too long: next model, limit learned -----------------------
    reset_health()
    a = FakeProvider("a", [err(400, "This model's maximum context length is 8192")])
    b = FakeProvider("b", ok_script("long ok"))
    orig, mod = patch_registry({"a": a, "b": b})
    try:
        route = R.Route(key="r", label="R", smart=False, candidates=[
            R.Candidate("a", "tiny"), R.Candidate("b", "huge")])
        big = [{"role": "user", "content": "x" * 60000}]
        ev = await collect(RoutedProvider(route), big)
        check("a context-length refusal fails over", text_of(ev) == "long ok")
        learned = R.caps("a", "tiny")["ctx"]
        check("...and a context limit is learned", bool(learned) and learned < 17000,
              str(learned))
        plan = R.plan(route, needs=R.Needs(tokens=16000))
        check("...so the next long request skips the small model",
              [c.model for c in plan.usable] == ["huge"])
    finally:
        mod.registry = orig

    # --- a rejected key rests that provider, the route carries on ---------
    reset_health()
    a = FakeProvider("a", [err(401, "invalid api key")])
    b = FakeProvider("b", ok_script("b answered"))
    orig, mod = patch_registry({"a": a, "b": b})
    try:
        route = R.Route(key="r", label="R", smart=False, candidates=[
            R.Candidate("a", "m1"), R.Candidate("a", "m2"), R.Candidate("b", "m3")])
        ev = await collect(RoutedProvider(route), [{"role": "user", "content": "hi"}])
        check("a 401 in a multi-provider route fails over", text_of(ev) == "b answered")
        check("...the provider is named in a notice",
              any("rejected its API key" in e.get("text", "") for e in ev))
        check("...its other models are skipped too (one call only)",
              len(a.calls) == 1, str(len(a.calls)))
        check("...and the whole provider rests", R.book.peek("a", "*").resting)
    finally:
        mod.registry = orig

    # --- a 401 with nowhere else to go still tells you ---------------------
    reset_health()
    a = FakeProvider("a", [err(401, "invalid api key")])
    orig, mod = patch_registry({"a": a})
    try:
        route = R.Route(key="r", label="R", candidates=[R.Candidate("a", "m1")])
        ev = await collect(RoutedProvider(route), [{"role": "user", "content": "hi"}])
        check("a 401 on a single-provider route surfaces as an error",
              any(e["type"] == "error" and e.get("kind") == R.AUTH for e in ev))
    finally:
        mod.registry = orig

    # --- an empty answer is a failure when there is someone else to ask ---
    reset_health()
    a = FakeProvider("a", [{"type": "done"}])
    b = FakeProvider("b", ok_script("not empty"))
    orig, mod = patch_registry({"a": a, "b": b})
    try:
        route = R.Route(key="r", label="R", smart=False, candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        ev = await collect(RoutedProvider(route), [{"role": "user", "content": "hi"}])
        check("an empty reply moves to the next model", text_of(ev) == "not empty")
        check("...exactly one done event reaches the loop",
              sum(1 for e in ev if e["type"] == "done") == 1)
        check("...and the empty model is rested briefly",
              R.book.get("a", "m1").resting)
    finally:
        mod.registry = orig

    # --- in-stream error chunks (OpenRouter style) --------------------------
    reset_health()
    from aegis.providers.openai_compat import OpenAICompatProvider

    class H(BaseHTTPRequestHandler):
        def do_POST(self):                  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = (b'data: {"choices":[{"delta":{"content":""}}]}\n\n'
                    b'data: {"error":{"code":429,"message":"Rate limit exceeded: free-models-per-day"}}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = OpenAICompatProvider("s", "S", f"http://127.0.0.1:{srv.server_address[1]}/v1",
                                 needs="")
        ev = [e async for e in p.chat([{"role": "user", "content": "x"}], "m")]
        errs = [e for e in ev if e["type"] == "error"]
        check("an error inside a 200 stream is surfaced", bool(errs), str(ev)[:200])
        check("...with its code, so it is classified as quota",
              errs and R.classify(errs[0].get("status"), errs[0]["text"]) == R.QUOTA,
              str(errs)[:200])
    finally:
        srv.shutdown()

    # --- overflow route: free spent -> the route you chose, announced ------
    reset_health()
    from aegis.config import settings
    a = FakeProvider("a", [err(402, "insufficient credit")])
    b = FakeProvider("b", ok_script("paid answer"))
    orig, mod = patch_registry({"a": a, "b": b})
    try:
        settings.set("routes", {
            "free": {"label": "Free", "allow_paid": False, "then_route": "paid",
                     "candidates": [{"provider": "a", "model": "m:free"}]},
            "paid": {"label": "Paid", "allow_paid": True,
                     "candidates": [{"provider": "b", "model": "m"}]},
        })
        route = R.get_route("free")
        ev = await collect(RoutedProvider(route), [{"role": "user", "content": "hi"}])
        check("a spent free route continues on its overflow route",
              text_of(ev) == "paid answer", str(ev)[:300])
        check("...and the switch to a paying route is announced",
              any("may cost money" in e.get("text", "") for e in ev))
        settings.set("routes", {
            "free": {"label": "Free", "allow_paid": False,
                     "candidates": [{"provider": "a", "model": "m:free"}]}})
        reset_health()
        ev = await collect(RoutedProvider(R.get_route("free")),
                           [{"role": "user", "content": "hi"}])
        check("without an overflow route, a free route still never pays",
              not b.calls[1:] and any(e["type"] == "error" for e in ev))
    finally:
        mod.registry = orig


# ---------------------------------------------------------------------------
# 3. Smart routing
# ---------------------------------------------------------------------------

async def _smart() -> None:
    from aegis import router as R
    from aegis import taskroute as TR
    from aegis.providers.routed import RoutedProvider

    p = TR.profile([{"role": "user", "content": "fix this python traceback in my script"}])
    check("code is recognised", p.kind == "code", p.kind)
    p = TR.profile([{"role": "user", "content": "what's up", "images": [("image/png", "x")]}])
    check("an attached image means vision", p.kind == "vision" and p.images)
    p = TR.profile([{"role": "user", "content": "summarise " + "word " * 30000}])
    check("a huge context means long", p.kind == "long", p.kind)
    p = TR.profile([{"role": "user", "content": "capital of France?"}])
    check("a short question is quick", p.kind == "quick", p.kind)
    p = TR.profile([{"role": "user", "content": "write an afro-pop song about city rain"}])
    check("a song request is creative", p.kind == "creative", p.kind)

    reset_health()
    cands = [R.Candidate("x", "meta-llama/llama-3.1-8b-instruct:free"),
             R.Candidate("x", "qwen/qwen3-coder:free"),
             R.Candidate("groq", "llama-3.1-8b-instant")]
    code_first = TR.order(cands, TR.Profile(kind="code"))
    check("a coder goes first for code", code_first[0].model == "qwen/qwen3-coder:free",
          str([c.model for c in code_first]))
    quick_first = TR.order(cands, TR.Profile(kind="quick"))
    check("a fast provider goes first for quick questions",
          quick_first[0].provider == "groq", str([c.provider for c in quick_first]))
    check("plain chat keeps your order",
          TR.order(cands, TR.Profile(kind="chat")) == cands)

    # A smart route actually uses the reorder, and says which model it picked.
    a = FakeProvider("a", ok_script("from general"))
    c = FakeProvider("c", ok_script("from coder"))
    orig, mod = patch_registry({"a": a, "c": c})
    try:
        route = R.Route(key="r", label="R", smart=True, candidates=[
            R.Candidate("a", "llama-3.1-8b"), R.Candidate("c", "qwen3-coder-480b")])
        ev = await collect(RoutedProvider(route),
                           [{"role": "user", "content": "refactor this function in python"}])
        check("a smart route sends code to the coder", text_of(ev) == "from coder")
        picks = [e for e in ev if e["type"] == "route_pick"]
        check("...and reports its pick", picks and "code" in picks[0]["text"],
              str(picks)[:200])
    finally:
        mod.registry = orig

    models = [{"id": f"vendor/model-{i}"} for i in range(20)] + [
        {"id": "qwen/qwen3-coder:free"}, {"id": "google/gemini-2.5-flash:free", "vision": True},
        {"id": "openai/text-embedding-3-small"}, {"id": "black-forest-labs/flux-schnell"},
        {"id": "deepseek/deepseek-r1:free"}, {"id": "meta/llama-3.1-8b-instant"}]
    picked = [m["id"] for m in TR.pick_for_route(models, 6)]
    check("route picking skips embedding and image models",
          not any("embedding" in m or "flux" in m for m in picked), str(picked))
    check("...includes a coder", "qwen/qwen3-coder:free" in picked, str(picked))
    check("...includes a vision model", "google/gemini-2.5-flash:free" in picked)
    check("...prefers strong models over alphabetical order",
          "deepseek/deepseek-r1:free" in picked)


# ---------------------------------------------------------------------------
# 4. Quick setup and the Auto route
# ---------------------------------------------------------------------------

async def _quick_setup() -> None:
    from aegis import autoroute, router as R
    from aegis.config import settings
    from aegis.providers import custom

    catalogue = {"data": [
        {"id": "qwen/qwen3-coder:free", "pricing": {"prompt": "0"},
         "supported_parameters": ["tools"],
         "architecture": {"input_modalities": ["text"]}, "context_length": 262000},
        {"id": "google/gemini-2.5-flash:free", "pricing": {"prompt": "0"},
         "supported_parameters": ["tools"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "anthropic/claude-sonnet-4.5", "pricing": {"prompt": "0.000003"},
         "supported_parameters": ["tools"]},
        {"id": "some/model-without-tools:free", "pricing": {"prompt": "0"},
         "supported_parameters": ["temperature"]},
    ]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                   # noqa: N802
            body = json.dumps(catalogue).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    saved_presets = list(custom.PRESETS)
    try:
        settings.set("custom_providers", {})
        settings.set("free_models", [])
        settings.set("default_route", "")
        custom.PRESETS[:] = [{**p, "base_url": base} if p["key"] == "openrouter" else p
                             for p in custom.PRESETS]
        result = await autoroute.quick_setup({"openrouter": "sk-test", "groq": ""})
        check("quick setup saves the provider with a key", result["saved"] == ["openrouter"],
              str(result)[:300])
        route = R.get_route("auto")
        models = [c.model for c in route.candidates]
        check("the Auto route is built from free models only",
              "anthropic/claude-sonnet-4.5" not in models and len(models) >= 2,
              str(models))
        check("...is smart and free-only", route.smart and not route.allow_paid)
        check("...and becomes the default route", settings.get("default_route") == "auto")
        check("catalogue capabilities are stored",
              R.caps("openrouter", "google/gemini-2.5-flash:free")["vision"] is True
              and R.caps("openrouter", "some/model-without-tools:free")["tools"] is False)
        check("...including context size",
              R.caps("openrouter", "qwen/qwen3-coder:free")["ctx"] == 262000)
        check("a no-tools model is skipped for tool requests up front",
              "some/model-without-tools:free" not in
              [c.model for c in R.plan(route, require_tools=True).usable])
        check("a v2 preset for 9Router points at its local default",
              next(p for p in saved_presets if p["key"] == "ninerouter")["base_url"]
              == "http://localhost:20128/v1")
        check("there is a Pollinations preset",
              any(p["key"] == "pollinations" for p in saved_presets))
        settings.set("auto_route_built", time.time() - 3 * 86400)
        again = await autoroute.refresh_if_stale()
        check("a stale Auto route refreshes itself", again and again.get("ok"),
              str(again)[:200])
        settings.set("auto_route_managed", False)
        settings.set("auto_route_built", 0)
        check("...but not once you have edited it",
              await autoroute.refresh_if_stale() is None)
    finally:
        custom.PRESETS[:] = saved_presets
        srv.shutdown()


# ---------------------------------------------------------------------------
# 5. Studio
# ---------------------------------------------------------------------------

async def _studio() -> None:
    from aegis import media, router as R
    from aegis.config import settings

    reset_health()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    calls: list[str] = []

    async def broken(b, req):
        calls.append("broken")
        raise media.Failed("HTTP 429: daily limit reached", 429)

    async def works(b, req):
        calls.append("works")
        return png, "image/png"

    async def paid(b, req):
        calls.append("paid")
        return png, "image/png"

    media.METHODS["t_broken"] = broken
    media.METHODS["t_works"] = works
    media.METHODS["t_paid"] = paid
    settings.set("media_backends", [
        {"id": "one", "kind": "image", "method": "t_broken", "paid": False},
        {"id": "unset", "kind": "image", "method": "openai_image",
         "provider": "nobody", "paid": False},
        {"id": "pay", "kind": "image", "method": "t_paid", "paid": True},
        {"id": "two", "kind": "image", "method": "t_works", "paid": False},
    ])
    settings.set("media_allow_paid", False)
    item = await media.generate("image", "a lion at sunset over Ouagadougou")
    check("image generation fails over to a working backend",
          item.get("backend") == "two", str(item)[:200])
    check("...a paid backend is skipped by default", "paid" not in calls, str(calls))
    check("...an unconfigured provider is skipped, not failed",
          any("not added" in t for t in item.get("tried", [])), str(item.get("tried")))
    check("...the failing backend is rested",
          R.book.peek("media:one", "image").resting)
    check("...the file is saved", Path(item["path"]).is_file())
    check("...and indexed", media.get(item["id"])["prompt"].startswith("a lion"))
    check("the mime type is sniffed", item["mime"] == "image/png")

    calls.clear()
    item2 = await media.generate("image", "again")
    check("a rested backend is not retried", "broken" not in calls, str(calls))

    tool = next(t for t in media.tools() if t.name == "media__generate_image")
    out = await tool.handler({"prompt": "a lighthouse, watercolour", "count": 2})
    check("the image tool can make variations",
          out["text"].count("[[media:") == 2, out["text"][:200])
    check("media tools run without asking (they only spend free credit)",
          tool.risk == "read")

    settings.set("media_backends", [
        {"id": "only", "kind": "image", "method": "t_broken", "paid": False}])
    reset_health()
    try:
        await media.generate("image", "x")
        check("when every backend fails, the reason is explained", False)
    except media.Failed as exc:
        check("when every backend fails, the reason is explained",
              "daily limit" in str(exc), str(exc))

    # Suno pack fallback uses the chat route and always yields something.
    async def fake_text(prompt, system=""):
        return "## Title\nCity Rain\n## Style prompt\nafro-pop, 118 bpm"
    original = media.quick_text
    media.quick_text = fake_text
    try:
        settings.set("media_backends", [
            {"id": "suno-pack", "kind": "music", "method": "suno_pack", "paid": False}])
        song = await media.generate("music", "afro-pop about city rain")
        check("music falls back to a Suno pack file",
              song["mime"] == "text/markdown"
              and "City Rain" in Path(song["path"]).read_text("utf-8"))
        check("...and is flagged as a fallback", song["meta"].get("fallback") is True)
    finally:
        media.quick_text = original

    # Pollinations method against a local fake.
    class H(BaseHTTPRequestHandler):
        def do_GET(self):                   # noqa: N802
            H.path_seen = self.path
            H.auth = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            body = b"\xff\xd8\xff" + b"1" * 100
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from aegis.providers import custom
        custom.save("pollinations", "Pollinations",
                    f"http://127.0.0.1:{srv.server_address[1]}/v1", free_tier=True)
        custom.set_api_key("pollinations", "sk_test")
        settings.set("media_backends", [
            {"id": "pol", "kind": "image", "method": "pollinations_image",
             "provider": "pollinations", "model": "flux"}])
        reset_health()
        item = await media.generate("image", "red car", width=640, height=360)
        check("the Pollinations image call uses /image/{prompt}",
              H.path_seen.startswith("/image/red%20car"), H.path_seen)
        check("...with size and model", "width=640" in H.path_seen
              and "model=flux" in H.path_seen)
        check("...and the saved key", H.auth == "Bearer sk_test")
        check("...and a free-tier provider makes the backend free",
              media.is_paid({"provider": "pollinations", "model": "flux"}) is False)
    finally:
        srv.shutdown()

    st = media.status()
    check("studio status lists methods", "slideshow" in st["methods"])


# ---------------------------------------------------------------------------
# 6. Coding and web tools, and the workspace fence
# ---------------------------------------------------------------------------

async def _code_and_web() -> None:
    from aegis import projects
    from aegis.config import settings
    from aegis.tools import code, files as ft, web

    settings.set("folders", [])
    settings.set("workspace_enabled", True)
    projects.set_current("", "")
    names = {t.name for t in ft.tools()}
    check("with nothing connected the agent still has its own workspace",
          "files__write" in names and "files__edit" in names, str(names))

    out = await code.run_command(command="echo hello-aegis && pwd")
    check("code__run runs in the general workspace",
          "hello-aegis" in out["text"] and str(projects.general_workspace()) in out["text"],
          out["text"][:200])
    check("...and reports the exit code", "[exit 0" in out["text"])
    bad = await code.run_command(command="exit 3")
    check("a failing command is an error with its code",
          bad["error"] and "[exit 3" in bad["text"])
    out = await code.run_python(code="print(6*7)")
    check("code__python runs a snippet", "42" in out["text"], out["text"][:200])
    slow = await code.run_command(command="sleep 10", timeout=5)
    check("a command past its timeout is stopped", "timed out" in slow["text"])
    for nasty in ("rm -rf /", "format c:", "diskpart", "shutdown /s /t 0"):
        refused = await code.run_command(command=nasty)
        check(f"'{nasty}' is refused even with approval",
              refused["error"] and "Refused" in refused["text"])
    tool = next(t for t in code.tools() if t.name == "code__run")
    check("code tools always ask first", tool.risk == "write")
    outside = await code._wrap(code.run_command)({"command": "ls", "cwd": "/etc"})
    check("a working directory outside the fence is refused",
          outside["error"] and "outside" in outside["text"], outside["text"][:120])

    target = projects.general_workspace() / "demo.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    res = ft._edit(path=str(target), old="return 1", new="return 2")
    check("files__edit replaces an exact snippet",
          "return 2" in target.read_text() and "-    return 1" in res["text"])
    res = ft._edit(path=str(target), old="nope", new="x")
    check("...and refuses when the snippet is missing", res.get("error"))
    target.write_text("a\na\n", encoding="utf-8")
    res = ft._edit(path=str(target), old="a", new="b")
    check("...and when it is ambiguous", res.get("error") and "2 times" in res["text"])

    page = ('<div class="result results_links"><a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.citizensinformation.ie%2Fen%2F&rut=x">'
            'Citizens <b>Information</b></a><a class="result__snippet">Public '
            'services &amp; rights</a></div>')
    parsed = web.parse_ddg(page)
    check("DuckDuckGo results are parsed and unwrapped",
          parsed and parsed[0]["url"] == "https://www.citizensinformation.ie/en/"
          and parsed[0]["title"] == "Citizens Information", str(parsed))
    title, text = web.html_to_text("<html><title>T</title><script>evil()</script>"
                                   "<h2>Head</h2><p>Body &amp; soul</p></html>")
    check("pages become readable text", title == "T" and "## Head" in text
          and "Body & soul" in text and "evil" not in text, text)
    refused = await web.fetch(url="http://127.0.0.1:8817/api/settings")
    check("fetching AEGIS's own API is refused", refused["error"])
    refused = await web.fetch(url="http://192.168.1.1/")
    check("fetching the LAN is refused", refused["error"])
    refused = await web.fetch(url="file:///etc/passwd")
    check("non-http URLs are refused", refused["error"])


# ---------------------------------------------------------------------------
# 7. Projects
# ---------------------------------------------------------------------------

async def _projects() -> None:
    from aegis import projects, scheduler, store
    from aegis.tools import files as ft

    store.connect()
    res = projects.save("Job hunt EMEA", description="Land a remote TSE role",
                        instructions="Always tailor to local employers.")
    check("a project is created", res["ok"], str(res))
    key = res["key"]
    ws = projects.workspace(key)
    check("...with a workspace and PROJECT.md", (ws / "PROJECT.md").is_file())
    check("...and a knowledge folder", (ws / "knowledge").is_dir())
    again = projects.save("Job hunt EMEA")
    check("a second project with the same name gets its own key",
          again["key"] != key, again["key"])
    (ws / "knowledge" / "cv.md").write_text("CV", encoding="utf-8")
    block = projects.system_block(key)
    check("the project's instructions, notes and files reach the prompt",
          "local employers" in block and "## Goal" in block
          and "knowledge/cv.md" in block, block[:300])

    chat = store.create_chat(title="t")
    projects.assign(chat, key)
    check("a chat can belong to a project", projects.project_of(chat) == key)
    check("...and is counted", projects.summary_one(key)["chats"] == 1)

    projects.set_current(key, chat)
    check("in a project, relative paths land in its workspace",
          ft.resolve("notes.txt").parent == ws.resolve())

    tool = next(t for t in projects.tools() if t.name == "project__schedule")
    out = await tool.handler({"name": "Daily jobs", "prompt": "Find 5 new TSE roles",
                              "cron": "0 8 * * 1-5"})
    check("the agent can schedule a task", "Scheduled 'Daily jobs'" in out["text"],
          out["text"])
    task = next(t for t in scheduler.tasks().values() if t.name == "Daily jobs")
    check("...inside the current project", task.project == key)
    check("...whose output goes to the project workspace",
          scheduler.output_folder(task) == ws / "task-output")

    create = next(t for t in projects.tools() if t.name == "project__create")
    out = await create.handler({"name": "Album 2027", "description": "Coupé-décalé EP"})
    check("the agent can create a project and move the chat into it",
          "Album 2027" in out["text"] and projects.project_of(chat) == "album-2027",
          out["text"])
    projects.set_current("", "")
    check("deleting a project keeps its files", projects.delete(key) and ws.is_dir())


# ---------------------------------------------------------------------------
# 8. Super Agent
# ---------------------------------------------------------------------------

async def _superagent() -> None:
    from aegis import superagent as SA
    from aegis.config import settings

    r = SA.route([{"role": "user", "content":
                   "vCenter VCSA certificate expired, vpxd won't start after reboot"}])
    check("VMware issues go to vmware-support", r.preload[:1] == ["vmware-support"],
          str(r.ranked[:3]))
    r = SA.route([{"role": "user", "content": "tailor my CV and cover letter for this job"}])
    check("CV work goes to job-search", "job-search" in r.preload, str(r.ranked[:3]))
    r = SA.route([{"role": "user", "content": "make me a thumbnail image and a voice-over"}])
    check("media requests go to the studio", "media-studio" in r.preload)
    r = SA.route([{"role": "user", "content": "write a python script to rename my files"}])
    check("code goes to the coding specialist", "coding" in r.preload)
    check("...and brings the code tools", "code" in (r.tool_servers or set()))
    r = SA.route([{"role": "user", "content": "hello"}])
    check("small talk preloads nothing", r.preload == [])
    check("...and leaves out computer and browser tools",
          "computer" not in r.tool_servers and "browser" not in r.tool_servers)
    r = SA.route([{"role": "user", "content": "open the website and click sign in"}])
    check("UI work brings browser and computer tools",
          {"browser", "computer"} <= r.tool_servers)
    r = SA.route([{"role": "user", "content": "set up a daily reminder every morning"}])
    check("scheduling goes to the planner", "planner" in r.preload)
    r = SA.route([{"role": "user", "content": "the vSAN disk group is degraded"},
                  {"role": "assistant", "content": "..."},
                  {"role": "user", "content": "and in French?"}])
    check("a short follow-up keeps the previous intent", "vmware-support" in r.preload)

    class T:
        def __init__(self, server): self.server, self.name = server, server + "__x"
    kept = SA.filter_tools([T("files"), T("computer"), T("gmail"), T("code")],
                           SA.route([{"role": "user", "content": "hi"}]))
    check("the tool diet keeps MCP servers and drops unneeded built-ins",
          {t.server for t in kept} == {"files", "gmail"}, str([t.server for t in kept]))

    import demo_profile
    demo_profile.fill()
    settings.set("profile_sharing", "lite")
    prompt = SA.system_prompt([{"role": "user", "content": "vCenter down"}], tools=[])
    check("the core prompt is always there", "You are AEGIS" in prompt)
    check("...with today's date", time.strftime("%Y") in prompt)
    check("...the specialist is preloaded", "Specialist loaded: vmware-support" in prompt)
    check("...only the lite profile goes to a cloud model",
          "Springfield" in prompt and "Household" not in prompt)
    local = SA.system_prompt([{"role": "user", "content": "hi"}], tools=[],
                             local_model=True)
    check("a local model gets the ground rules", "Ground rules" in local)
    settings.set("profile_sharing", "none")
    none = SA.system_prompt([{"role": "user", "content": "hi"}], tools=[],
                            local_model=True)
    check("profile sharing can be switched off", "Springfield" not in none
          and "Ground rules" not in none)
    settings.set("profile_sharing", "lite")


# ---------------------------------------------------------------------------
# 9. A whole run inside a project, end to end through the API
# ---------------------------------------------------------------------------

def _end_to_end() -> None:
    from fastapi.testclient import TestClient

    from aegis import projects, providers as provider_mod
    from aegis.server import app

    seen: dict[str, Any] = {}

    class P(FakeProvider):
        async def chat(self, messages, model, **opts):
            seen["system"] = messages[0]["content"]
            seen["tools"] = [t.name for t in (opts.get("tools") or [])]
            yield {"type": "delta", "text": "done"}
            yield {"type": "done"}

    fake = P("fake")
    original = provider_mod.registry
    provider_mod.registry = lambda include_routes=False: {"fake": fake}
    try:
        with TestClient(app) as client:
            r = client.post("/api/projects", json={
                "name": "AEGIS dev", "instructions": "Use FastAPI.",
                "skills": ["coding"]})
            check("projects can be created over the API", r.status_code == 200, r.text)
            key = r.json()["key"]
            r = client.post("/api/chat", json={
                "provider": "fake", "model": "m", "project": key,
                "text": "fix the bug in server.py"})
            chat_id = r.headers.get("X-Aegis-Chat-Id")
            body = r.text
            check("the chat runs", "done" in body, body[:200])
            check("the system prompt carries the project",
                  "Use FastAPI." in seen.get("system", ""))
            check("...and the project's pinned skill",
                  "Specialist loaded: coding" in seen.get("system", ""))
            check("...and the code tools are offered", "code__run" in seen.get("tools", []))
            check("the chat is filed under the project",
                  projects.project_of(chat_id) == key)
            chats = client.get("/api/chats").json()["chats"]
            check("the chat list shows the project",
                  any(c["id"] == chat_id and c["project"] == key for c in chats))
            check("the specialist pick is in the event stream",
                  "agent_pick" in body)
            r = client.get("/api/superagent", params={"text": "esxi psod"})
            check("the routing preview endpoint works",
                  r.json()["routing"]["preload"][:1] == ["vmware-support"])
            r = client.get("/api/media-backends")
            check("the studio endpoint lists backends", "backends" in r.json())
            r = client.get("/api/setup/status")
            check("the setup status endpoint works", "quick_presets" in r.json())
            r = client.post("/api/setup/quick", json={"keys": {}})
            check("quick setup with no keys is refused politely", r.status_code == 400)
    finally:
        provider_mod.registry = original


def run_all() -> None:
    section("model-specific refusals are classified")
    _classify()
    section("never-error routing")
    asyncio.run(_never_error())
    section("smart routing")
    asyncio.run(_smart())
    section("quick setup and the Auto route")
    asyncio.run(_quick_setup())
    section("Studio: images, speech, music, video")
    asyncio.run(_studio())
    section("coding and web tools")
    asyncio.run(_code_and_web())
    section("projects")
    asyncio.run(_projects())
    section("Super Agent")
    asyncio.run(_superagent())
    section("end to end")
    _end_to_end()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-v2-test-"))
    from harness import report
    run_all()
    sys.exit(report())
