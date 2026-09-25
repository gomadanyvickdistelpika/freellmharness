"""Model routing: classification, cooldowns, failover and the free/paid wall.

The tests that matter here are the ones that prove the *distinction*: a quota
failure must move to the next model, and a malformed request must not - burning
through five models on a request none of them can answer is worse than one
honest error.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402


class FakeProvider:
    """A provider that fails however the test needs it to."""
    kind = "api"
    supports_tools = True
    supports_images = True
    blurb = ""
    needs = ""

    def __init__(self, key: str, script: list[dict[str, Any]],
                 kind: str = "api", supports_tools: bool = True) -> None:
        self.key = key
        self.label = key
        self.kind = kind
        self.supports_tools = supports_tools
        self.script = script
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["m"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts) -> AsyncIterator[dict[str, Any]]:
        self.calls.append((model, [dict(m) for m in messages]))
        for event in self.script:
            yield event


def patch_registry(providers: dict[str, Any]):
    """Swap the provider registry for a fixed set, and give it back after."""
    from aegis import providers as provider_mod

    original = provider_mod.registry
    provider_mod.registry = lambda include_routes=False: dict(providers)
    return original, provider_mod


def restore_registry(original, module) -> None:
    module.registry = original


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------

def _classify() -> None:
    from aegis import router as R

    cases = [
        (402, "", R.QUOTA, "402 is out of credit"),
        (401, "", R.AUTH, "401 is an auth problem"),
        (403, "", R.AUTH, "403 is an auth problem"),
        (404, "", R.NOT_FOUND, "404 means the model is gone"),
        (429, "", R.RATE_LIMIT, "plain 429 is a rate limit"),
        (500, "", R.SERVER, "500 is a server fault"),
        (503, "", R.SERVER, "503 is a server fault"),
        (400, "", R.BAD_REQUEST, "400 is our own bad request"),
    ]
    for status, text, expected, label in cases:
        check(label, R.classify(status, text) == expected,
              R.classify(status, text))

    # The one that actually bites: a 429 that really means "you are out of
    # free allowance for today" deserves a long rest, not a 60-second one.
    check("a 429 saying the free tier is spent counts as quota",
          R.classify(429, "You have exceeded your free-tier limit for today")
          == R.QUOTA)
    check("a 400 mentioning credit counts as quota",
          R.classify(400, "Your credit balance is too low") == R.QUOTA)

    check("text-only quota wording is caught",
          R.classify(None, "insufficient_quota") == R.QUOTA)
    check("text-only rate wording is caught",
          R.classify(None, "Rate limit reached, try again later") == R.RATE_LIMIT)
    check("connection errors are timeouts",
          R.classify(None, "ConnectError: connection refused") == R.TIMEOUT)
    check("read timeouts are timeouts",
          R.classify(None, "ReadTimeout") == R.TIMEOUT)
    check("missing model wording is caught",
          R.classify(None, "The model `foo` does not exist") == R.NOT_FOUND)
    check("anything else is unknown", R.classify(None, "banana") == R.UNKNOWN)

    for kind in (R.QUOTA, R.RATE_LIMIT, R.SERVER, R.TIMEOUT, R.NOT_FOUND):
        check(f"{kind} fails over", R.should_failover(kind) is True)
    for kind in (R.AUTH, R.BAD_REQUEST, R.UNKNOWN):
        check(f"{kind} does NOT fail over", R.should_failover(kind) is False)

    check("Retry-After is read out of an error body",
          R.retry_after_seconds('{"retry_after": 42}') == 42.0)
    check("no Retry-After returns nothing",
          R.retry_after_seconds("nothing here") is None)


# ---------------------------------------------------------------------------
# 2. Health
# ---------------------------------------------------------------------------

def _health() -> None:
    from aegis import router as R
    from aegis.config import settings

    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    health = R.book.get("p", "m")
    check("a fresh model is healthy", not health.resting)

    R.book.record_failure("p", "m", R.RATE_LIMIT, "rate limited")
    health = R.book.get("p", "m")
    check("a rate limit starts a short rest",
          health.resting and 0 < health.rest_remaining <= R.COOLDOWN[R.RATE_LIMIT],
          f"{health.rest_remaining}s")

    R.book.record_failure("p", "m", R.RATE_LIMIT, "again")
    second = R.book.get("p", "m").rest_remaining
    check("repeat failures back off further",
          second > R.COOLDOWN[R.RATE_LIMIT], f"{second}s")

    R.book.record_failure("p", "q", R.QUOTA, "out of credit")
    check("a quota failure rests much longer than a rate limit",
          R.book.get("p", "q").rest_remaining > R.book.get("p", "m").rest_remaining
          or R.book.get("p", "q").rest_remaining > R.COOLDOWN[R.RATE_LIMIT] * 4)

    R.book.record_failure("p", "r", R.RATE_LIMIT, '{"retry_after": 300}')
    check("Retry-After lengthens the rest when the provider asks",
          R.book.get("p", "r").rest_remaining > 200,
          f"{R.book.get('p', 'r').rest_remaining}s")

    R.book.record_success("p", "m", output_tokens=500, seconds=2.0)
    health = R.book.get("p", "m")
    check("success clears the cooldown", not health.resting)
    check("success resets the failure streak", health.consecutive_failures == 0)
    check("tokens per second are measured", abs(health.tps - 250.0) < 1,
          f"{health.tps}")

    R.book.record_success("p", "m", output_tokens=100, seconds=2.0)
    check("speed is smoothed, not replaced",
          150 < R.book.get("p", "m").tps < 250, f"{R.book.get('p', 'm').tps}")

    check("health persists to settings",
          "p::m" in (settings.get("model_health") or {}))

    check("revive lifts every cooldown", R.book.revive_all() >= 1)
    check("nothing is resting after a revive",
          not any(h["resting"] for h in R.book.all().values()))
    check("speed survives a revive", R.book.get("p", "m").tps > 0)


# ---------------------------------------------------------------------------
# 3. Planning: order, resting, free/paid
# ---------------------------------------------------------------------------

def _planning() -> None:
    from aegis import router as R
    from aegis.config import settings

    settings.set("model_health", {})
    settings.set("free_models", ["freeco::small"])
    R.book._data.clear()
    R.book._loaded = True

    fakes = {
        "freeco": FakeProvider("freeco", []),
        "paidco": FakeProvider("paidco", []),
        "localco": FakeProvider("localco", [], kind="local"),
        "notools": FakeProvider("notools", [], supports_tools=False),
    }
    original, module = patch_registry(fakes)
    try:
        check("a local provider is never paid", R.is_paid("localco", "x") is False)
        check("a tagged free model is not paid", R.is_paid("freeco", "small") is False)
        check("a ':free' suffix means free", R.is_paid("paidco", "m:free") is False)
        check("anything else is assumed paid", R.is_paid("paidco", "big") is True)

        route = R.Route(key="t", label="T", allow_paid=True, candidates=[
            R.Candidate("freeco", "small"),
            R.Candidate("paidco", "big"),
            R.Candidate("ghostco", "x"),
        ])
        plan = R.plan(route)
        check("candidates keep the order you gave them",
              [c.provider for c in plan.usable] == ["freeco", "paidco"],
              str([c.provider for c in plan.usable]))
        check("an unknown provider is excluded with a reason",
              plan.excluded and plan.excluded[0][1] == "provider not configured")

        R.book.record_failure("freeco", "small", R.QUOTA, "spent")
        plan = R.plan(route)
        check("a resting model drops out of the usable list",
              [c.provider for c in plan.usable] == ["paidco"])
        check("the resting model is reported, not silently dropped",
              plan.resting and plan.resting[0][0].provider == "freeco")
        check("the explanation names what it is resting on",
              "quota" in plan.explain(), plan.explain())

        free_route = R.Route(key="f", label="F", allow_paid=False, candidates=[
            R.Candidate("paidco", "big"),
            R.Candidate("localco", "tiny"),
        ])
        plan = R.plan(free_route)
        check("a free-only route excludes paid models",
              [c.provider for c in plan.usable] == ["localco"],
              str([c.provider for c in plan.usable]))
        check("the exclusion says why",
              any("free-only" in why for _, why in plan.excluded))

        tool_route = R.Route(key="x", label="X", allow_paid=True, candidates=[
            R.Candidate("notools", "m"), R.Candidate("paidco", "big")])
        plan = R.plan(tool_route, require_tools=True)
        check("a model that cannot use tools is skipped when tools are needed",
              [c.provider for c in plan.usable] == ["paidco"])
        plan = R.plan(tool_route, require_tools=False)
        check("...but kept when they are not",
              len(plan.usable) == 2)

        empty = R.Route(key="e", label="E", candidates=[])
        check("an empty route is not ok", R.plan(empty).ok is False)
    finally:
        restore_registry(original, module)
        settings.set("free_models", [])


# ---------------------------------------------------------------------------
# 4. RoutedProvider: the actual failover behaviour
# ---------------------------------------------------------------------------

def err(status: int | None, text: str) -> dict[str, Any]:
    return {"type": "error", "status": status, "text": text}


async def _failover() -> None:
    from aegis import router as R
    from aegis.config import settings
    from aegis.providers.routed import RoutedProvider

    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    # --- quota on the first model: switch, silently ------------------------
    first = FakeProvider("a", [err(402, "insufficient_quota")])
    second = FakeProvider("b", [{"type": "delta", "text": "hello from b"},
                                {"type": "usage", "input": 1, "output": 3},
                                {"type": "done"}])
    original, module = patch_registry({"a": first, "b": second})
    try:
        route = R.Route(key="r", label="R", candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]
        text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        kinds = [e["type"] for e in events]

        check("the answer comes from the second model", text == "hello from b", text)
        check("the user never sees an error", "error" not in kinds, str(kinds))
        check("the switch is mentioned once",
              sum(1 for e in events if e["type"] == "notice") == 1, str(kinds))
        check("the failed model is put to rest",
              R.book.get("a", "m1").resting)
        check("the working model is recorded as healthy",
              R.book.get("b", "m2").successes == 1)
        check("the second model got the original question untouched",
              second.calls[0][1][-1]["content"] == "hi")
    finally:
        restore_registry(original, module)

    # --- a bad request must NOT walk the whole list ------------------------
    R.book._data.clear()
    bad = FakeProvider("a", [err(400, "messages: invalid role 'banana'")])
    never = FakeProvider("b", [{"type": "delta", "text": "should not run"},
                               {"type": "done"}])
    original, module = patch_registry({"a": bad, "b": never})
    try:
        route = R.Route(key="r", label="R", candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]
        check("a bad request surfaces instead of failing over",
              any(e["type"] == "error" for e in events))
        check("the second model is never called on a bad request",
              never.calls == [], str(len(never.calls)))
        check("the error says what kind it was",
              any(e.get("kind") == R.BAD_REQUEST for e in events
                  if e["type"] == "error"))
    finally:
        restore_registry(original, module)

    # --- mid-stream death: keep the text, continue elsewhere ---------------
    R.book._data.clear()
    dies = FakeProvider("a", [{"type": "delta", "text": "The capital of France "},
                              err(503, "upstream overloaded")])
    carries = FakeProvider("b", [{"type": "delta", "text": "is Paris."},
                                 {"type": "done"}])
    original, module = patch_registry({"a": dies, "b": carries})
    try:
        route = R.Route(key="r", label="R", candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "capital of France?"}], "")]
        text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        check("the partial answer is kept, not thrown away",
              text == "The capital of France is Paris.", repr(text))

        continued = carries.calls[0][1]
        check("the second model is shown what was already written",
              any(m["role"] == "assistant" and "capital of France" in m["content"]
                  for m in continued))
        check("the second model is told to continue, not restart",
              "continue" in continued[-1]["content"].lower())
        check("the break is visible in the trace",
              any("stopped" in e.get("text", "") for e in events
                  if e["type"] == "notice"))
    finally:
        restore_registry(original, module)

    # --- a half-built tool call restarts rather than continuing ------------
    R.book._data.clear()
    toolish = FakeProvider("a", [{"type": "delta", "text": "let me check "},
                                 {"type": "tool_calls", "calls": [
                                     {"id": "1", "name": "x", "arguments": {}}]},
                                 err(500, "boom")])
    clean = FakeProvider("b", [{"type": "delta", "text": "fresh start"},
                               {"type": "done"}])
    original, module = patch_registry({"a": toolish, "b": clean})
    try:
        route = R.Route(key="r", label="R", candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        _ = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]
        sent = clean.calls[0][1]
        check("a broken tool call is not continued from",
              not any(m["role"] == "assistant" and "let me check" in str(m.get("content"))
                      for m in sent))
    finally:
        restore_registry(original, module)

    # --- everything exhausted ---------------------------------------------
    R.book._data.clear()
    dead_a = FakeProvider("a", [err(429, "rate limited")])
    dead_b = FakeProvider("b", [err(402, "insufficient_quota")])
    original, module = patch_registry({"a": dead_a, "b": dead_b})
    try:
        route = R.Route(key="r", label="R", allow_paid=False, candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        settings.set("free_models", ["a::m1", "b::m2"])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]
        error = next(e for e in events if e["type"] == "error")
        check("when all models fail you get one clear error",
              "Every model" in error["text"], error["text"][:80])
        check("a free route explains it will not spend money",
              "free-only" in error["text"] or "paid route" in error["text"],
              error["text"][-120:])

        # And the next attempt does not even try them.
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "again"}], "")]
        check("resting models are not retried while they rest",
              len(dead_a.calls) == 1 and len(dead_b.calls) == 1,
              f"a={len(dead_a.calls)} b={len(dead_b.calls)}")
        check("the second attempt says nothing is available",
              any("No model" in e.get("text", "") for e in events
                  if e["type"] == "error"))
    finally:
        restore_registry(original, module)
        settings.set("free_models", [])

    # --- a free route refuses paid models even when starved ----------------
    R.book._data.clear()
    paid = FakeProvider("paid", [{"type": "delta", "text": "expensive"},
                                 {"type": "done"}])
    original, module = patch_registry({"paid": paid})
    try:
        route = R.Route(key="f", label="Free", allow_paid=False,
                        candidates=[R.Candidate("paid", "gpt-expensive")])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]
        check("a free route will not run a paid model to avoid failing",
              paid.calls == [] and any(e["type"] == "error" for e in events))
    finally:
        restore_registry(original, module)


# ---------------------------------------------------------------------------
# 5. Custom providers
# ---------------------------------------------------------------------------

def _custom() -> None:
    from aegis import vault
    from aegis.config import settings
    from aegis.providers import custom

    settings.set("custom_providers", {})

    bad = custom.save("or", "OpenRouter", "openrouter.ai/api/v1")
    check("a base URL without a scheme is refused", bad["ok"] is False, str(bad))
    check("an empty key is refused", custom.save("", "x", "https://x")["ok"] is False)

    result = custom.save("openrouter", "OpenRouter",
                         "https://openrouter.ai/api/v1/", note="test")
    check("a provider saves", result["ok"] is True)
    check("the trailing slash is trimmed",
          custom.stored()["openrouter"]["base_url"].endswith("/v1"))

    custom.set_api_key("openrouter", "sk-or-secret-123456")
    check("the key goes in the vault, not settings",
          "sk-or-secret-123456" not in str(settings.all()))
    check("the key is retrievable",
          vault.get(custom.key_name("openrouter")) == "sk-or-secret-123456")

    built = {p.key: p for p in custom.build()}
    check("a Provider is built from it", "openrouter" in built)
    check("it points at the right URL",
          built["openrouter"].base_url == "https://openrouter.ai/api/v1")
    check("it can use tools", built["openrouter"].supports_tools is True)

    summary = custom.summary()
    entry = next(p for p in summary["providers"] if p["key"] == "openrouter")
    check("the summary masks the key", "secret" not in entry["masked"],
          entry["masked"])
    check("presets are offered", len(summary["presets"]) >= 6)
    check("a blank-URL preset exists for anything else",
          any(p["base_url"] == "" for p in summary["presets"]))

    check("removing takes the key with it",
          custom.remove("openrouter") is True
          and vault.get(custom.key_name("openrouter")) is None)

    # Free detection from a /models payload shape.
    check("a zero price is free",
          custom._price_of({"pricing": {"prompt": "0"}}) == 0.0)
    check("a real price is read",
          custom._price_of({"pricing": {"prompt": "0.0000005"}}) == 5e-7)
    check("no pricing block returns nothing", custom._price_of({}) is None)


async def _no_models_endpoint() -> None:
    """Not every OpenAI-compatible service publishes /models. Atria does not.

    Probing for one and failing would report a perfectly good provider as
    broken, and leave its model list empty so no route could use it.
    """
    from aegis import vault
    from aegis.config import settings
    from aegis.providers import custom
    from aegis.providers.openai_compat import _retry_after_header

    settings.set("custom_providers", {})

    refused = custom.save("atria", "Atria", "https://api.atria-asi.ai/v1",
                          has_models_endpoint=False, models=[])
    check("a provider with no /models must list its models by hand",
          refused["ok"] is False, str(refused))

    saved = custom.save("atria", "Atria", "https://api.atria-asi.ai/v1",
                        has_models_endpoint=False, models=["Atria-Dawn-Preview"])
    check("...and saves once it does", saved["ok"] is True, str(saved))

    provider = {p.key: p for p in custom.build()}["atria"]
    check("the provider knows it has no /models endpoint",
          provider.has_models_endpoint is False)

    models = await provider.models()
    check("models come from the hand-entered list, not the network",
          models == ["Atria-Dawn-Preview"], str(models))
    check("the model id keeps its capitalisation",
          models[0] == "Atria-Dawn-Preview", models[0])

    vault.put(custom.key_name("atria"), "test-key")
    state = await provider.status()
    check("a keyed provider with no /models still reports ready",
          state["ready"] is True, state["detail"])
    check("...and says why it cannot verify",
          "no /models endpoint" in state["detail"], state["detail"])

    vault.delete(custom.key_name("atria"))
    state = await provider.status()
    check("without a key it is still not ready", state["ready"] is False)

    listing = await custom.list_models("atria")
    check("listing models returns the hand-entered set",
          listing["ok"] and listing["count"] == 1, str(listing))
    check("...and admits it cannot tell free from paid",
          listing["priced"] is False and "cannot tell" in listing.get("note", ""),
          listing.get("note", "")[:60])

    preset = next((p for p in custom.PRESETS if p["key"] == "atria"), None)
    check("Atria ships as a preset", preset is not None)
    check("the preset carries the documented base URL",
          preset["base_url"] == "https://api.atria-asi.ai/v1", preset["base_url"])
    check("the preset pre-fills the model id",
          preset.get("models") == "Atria-Dawn-Preview")

    xkiro = next((p for p in custom.PRESETS if p["key"] == "xkiro"), None)
    check("xKiro ships as a preset", xkiro is not None)
    check("xKiro carries its documented base URL",
          xkiro["base_url"] == "https://api.xkiro.com/v1", xkiro["base_url"])
    check("xKiro is not marked as lacking /models (it has one)",
          xkiro.get("has_models") != "no")

    openrouter = next((p for p in custom.PRESETS if p["key"] == "openrouter"), None)
    check("OpenRouter carries its documented base URL",
          openrouter["base_url"] == "https://openrouter.ai/api/v1")

    # Retry-After, which Atria sends on 429.
    check("Retry-After in seconds is read",
          _retry_after_header({"retry-after": "30"}) == 30.0)
    check("a missing Retry-After returns nothing",
          _retry_after_header({}) is None)
    check("a nonsense Retry-After does not raise",
          _retry_after_header({"retry-after": "soon"}) is None)

    custom.remove("atria")
    settings.set("custom_providers", {})


async def _unpriced_catalogue() -> None:
    """A catalogue with no prices must not wipe the tags you set by hand.

    xKiro publishes /v1/models but no per-model pricing - on xKiro "free" is a
    daily allowance on your plan, not a property of the model. If listing models
    overwrote the free tags every time, a free route would silently empty itself
    the next time you pressed refresh.

    This runs a real HTTP server serving a real, price-free /models payload.
    """
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from aegis.config import settings
    from aegis.providers import custom

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    payload = {"data": [{"id": "openai/gpt-5.6-sol"},
                        {"id": "deepseek/deepseek-chat"},
                        {"id": "qwen/qwen3-8b"}]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):          # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        settings.set("custom_providers", {})
        settings.set("free_models", [])
        custom.save("xkiro", "xKiro", f"http://127.0.0.1:{port}/v1")

        listing = await custom.list_models("xkiro")
        check("an unpriced catalogue still lists its models",
              listing["ok"] and listing["count"] == 3, str(listing)[:120])
        check("...and reports that no prices were published",
              listing["priced"] is False)
        check("...so nothing is assumed free", listing["free"] == 0)
        check("...and it says what to do about it",
              "mark" in listing.get("note", "").lower(), listing.get("note", "")[:60])

        # Tag one by hand, the way the UI does.
        settings.set("free_models", ["xkiro::qwen/qwen3-8b"])
        again = await custom.list_models("xkiro")
        tagged = {m["id"]: m["free"] for m in again["models"]}
        check("a hand-set free tag survives a refresh",
              tagged["qwen/qwen3-8b"] is True, str(tagged))
        check("untagged models stay paid",
              tagged["openai/gpt-5.6-sol"] is False)
        check("the tag is still in settings after listing",
              "xkiro::qwen/qwen3-8b" in (settings.get("free_models") or []))
        check("free models sort to the top",
              again["models"][0]["id"] == "qwen/qwen3-8b",
              again["models"][0]["id"])

        # A priced catalogue is allowed to overwrite, since it knows better.
        payload["data"] = [{"id": "a/b", "pricing": {"prompt": "0"}},
                           {"id": "c/d", "pricing": {"prompt": "0.000002"}}]
        priced = await custom.list_models("xkiro")
        check("a priced catalogue is trusted to set the tags",
              priced["priced"] is True and priced["free"] == 1, str(priced["free"]))
        check("...and replaces the stale hand tags for that provider",
              "xkiro::qwen/qwen3-8b" not in (settings.get("free_models") or []))
    finally:
        server.shutdown()
        settings.set("custom_providers", {})
        settings.set("free_models", [])


async def _free_tier_and_autobuild() -> None:
    """A whole-account free plan, and building a route out of every free source.

    xKiro, 9Router and an Atria preview key all work the same way: the allowance
    belongs to the plan, not to any one model, and the catalogue carries no
    prices to prove it. Marking the provider free-tier is the user asserting
    something about their own account, and it has to be enough to let a
    free-only route use every model on it without tagging them one by one.
    """
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from aegis import router as R
    from aegis.config import settings
    from aegis.providers import custom

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    payload = {"data": [{"id": "openai/gpt-5.6-sol"}, {"id": "qwen/qwen3-8b"},
                        {"id": "deepseek/deepseek-chat"}]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        settings.set("custom_providers", {})
        settings.set("free_models", [])

        custom.save("xkiro", "xKiro", f"http://127.0.0.1:{port}/v1",
                    free_tier=True)
        check("free-tier is remembered on the provider",
              custom.is_free_tier("xkiro") is True)
        check("...and is reported back to the UI",
              next(p for p in custom.summary()["providers"]
                   if p["key"] == "xkiro")["free_tier"] is True)

        listing = await custom.list_models("xkiro")
        check("every model on a free-tier account lists as free",
              listing["free"] == listing["count"] == 3, str(listing)[:120])
        check("...and it says why, rather than claiming to know the prices",
              "free-tier" in listing.get("note", ""), listing.get("note", ""))
        check("a free-tier model is not treated as paid",
              R.is_paid("xkiro", "openai/gpt-5.6-sol") is False)
        check("a model on some other provider is still assumed paid",
              R.is_paid("elsewhere", "openai/gpt-5.6-sol") is True)

        custom.save("xkiro", "xKiro", f"http://127.0.0.1:{port}/v1",
                    free_tier=False)
        check("turning free-tier off makes them paid again",
              R.is_paid("xkiro", "openai/gpt-5.6-sol") is True)
        check("...and the listing stops calling them free",
              (await custom.list_models("xkiro"))["free"] == 0)

        # --- autobuild ----------------------------------------------------
        # Which local engines exist has to be controlled, not inherited from
        # whichever machine runs this. On a laptop with Ollama up, autobuild
        # correctly finds a local model and every count below shifts by one -
        # which is the feature working and the test being wrong. So the local
        # side is stubbed, and tested deliberately further down.
        from fastapi.testclient import TestClient

        from aegis import providers as provider_mod
        from aegis.server import app

        class FakeLocal:
            kind = "local"
            supports_tools = True
            supports_images = False
            blurb = ""
            needs = ""

            def __init__(self, key: str, models: list[str]) -> None:
                self.key = key
                self.label = key
                self._models = models

            async def status(self): return {"ready": True, "detail": "", "hint": ""}
            async def models(self): return list(self._models)
            def describe(self): return {"key": self.key}

        real_get = provider_mod.get
        local_engines: dict[str, Any] = {}
        provider_mod.get = lambda key: local_engines.get(key) or (
            None if key in ("ollama", "lmstudio") else real_get(key))

        try:
            with TestClient(app) as client:
                r = client.post("/api/routes/autobuild", json={"key": "test"})
                check("autobuild refuses when there is nothing free to add",
                      r.status_code == 400, str(r.status_code))
                check("...and says what to do about it",
                      "free-tier" in r.json().get("error", ""), r.text[:140])

                custom.save("xkiro", "xKiro", f"http://127.0.0.1:{port}/v1",
                            free_tier=True)
                r = client.post("/api/routes/autobuild",
                                json={"key": "test", "per_provider": 2})
                body = r.json()
                check("autobuild builds a route from the free provider",
                      body.get("ok") is True and body["added"] == 2, r.text[:200])

                route = body["route"]
                check("the built route is free-only",
                      route["allow_paid"] is False)
                check("...and every candidate on it really is free",
                      all(not R.is_paid(c["provider"], c["model"])
                          for c in route["candidates"]))
                check("...and each one says where it came from",
                      all("free on xKiro" in c["note"]
                          for c in route["candidates"]),
                      str(route["candidates"])[:160])
                check("per_provider caps how many are taken from each",
                      len(route["candidates"]) == 2)

                # A second free provider extends the same route, so one running
                # out falls through to the other.
                custom.save("nine", "9Router", f"http://127.0.0.1:{port}/v1",
                            free_tier=True)
                body = client.post("/api/routes/autobuild",
                                   json={"key": "test", "per_provider": 1}).json()
                used = [c["provider"] for c in body["route"]["candidates"]]
                check("every free provider ends up on the one route",
                      set(used) == {"xkiro", "nine"}, str(used))
                check("the route is ready to use straight away",
                      client.get("/api/routes").json()["routes"] is not None)

                # --- the ordering claim, with a local engine present --------
                # Free hosted models first because they are far faster than a
                # CPU-only local model; local last because it is the one thing
                # that can never run out.
                local_engines["ollama"] = FakeLocal("ollama", ["qwen2.5:7b",
                                                               "llama3.2:3b"])
                body = client.post("/api/routes/autobuild",
                                   json={"key": "test", "per_provider": 2}).json()
                order = [c["provider"] for c in body["route"]["candidates"]]
                check("a running local engine is picked up automatically",
                      "ollama" in order, str(order))
                check("...and is put last, after every hosted free model",
                      order.index("ollama") == len(order) - order.count("ollama"),
                      str(order))
                check("...with hosted models ahead of it",
                      all(p != "ollama" for p in order[:-2]), str(order))
                check("the counts are reported separately",
                      body["cloud"] == 4 and body["local"] == 2,
                      f"cloud={body['cloud']} local={body['local']}")
                local_note = next(c["note"] for c in body["route"]["candidates"]
                                  if c["provider"] == "ollama")
                check("...and the local one says why it is there",
                      "never runs out" in local_note, local_note)

                # A local engine alone is enough: nothing free hosted, but the
                # machine's own model still makes a usable free route.
                custom.remove("xkiro")
                custom.remove("nine")
                body = client.post("/api/routes/autobuild",
                                   json={"key": "test"}).json()
                check("a local engine alone still builds a route",
                      body.get("ok") is True and body["cloud"] == 0,
                      str(body)[:140])

                client.delete("/api/routes/test")
        finally:
            provider_mod.get = real_get
    finally:
        server.shutdown()
        settings.set("custom_providers", {})
        settings.set("free_models", [])


def _daily_quota_rests_until_midnight() -> None:
    """A spent daily allowance comes back at midnight, not in half an hour.

    Without this, a model that said "you have used your free tokens for today"
    gets retried every thirty minutes until bedtime - dozens of requests that
    cannot possibly succeed, on a key that is already being rate-limited.
    """
    from aegis import router as R
    from aegis.config import settings

    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    ordinary = R.book.record_failure("p", "plain", R.QUOTA,
                                     "insufficient_quota: out of credit")
    check("an ordinary quota failure rests for the usual half hour",
          25 * 60 <= ordinary.rest_remaining <= 35 * 60,
          f"{ordinary.rest_remaining}s")

    daily = R.book.record_failure("p", "daily", R.QUOTA,
                                  "You have used your daily free token "
                                  "allowance. Resets at midnight.")
    check("a daily allowance rests much longer than half an hour",
          daily.rest_remaining > 35 * 60, f"{daily.rest_remaining}s")
    check("...but never longer than a day",
          daily.rest_remaining <= 24 * 60 * 60 + 120, f"{daily.rest_remaining}s")
    check("...and it says it was a daily cap, not a generic one",
          "daily" in daily.last_kind, daily.last_kind)

    expected = R.seconds_until_midnight()
    check("the rest lands on the next local midnight",
          abs(daily.rest_remaining - expected) <= 5,
          f"{daily.rest_remaining} vs {expected:.0f}")

    # An explicit Retry-After still wins: the provider knows better than we do.
    told = R.book.record_failure("p", "told", R.QUOTA,
                                 "daily limit reached", retry_after=90)
    check("an explicit Retry-After beats the midnight guess",
          80 <= told.rest_remaining <= 100, f"{told.rest_remaining}s")


def _retry_after_wins() -> None:
    """When a provider says when to come back, believe it over our guess."""
    from aegis import router as R
    from aegis.config import settings

    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    R.book.record_failure("atria", "m", R.RATE_LIMIT, "429")
    guessed = R.book.get("atria", "m").rest_remaining

    R.book._data.clear()
    R.book.record_failure("atria", "m", R.RATE_LIMIT, "429", retry_after=300)
    honoured = R.book.get("atria", "m").rest_remaining
    check("a Retry-After header overrides the default cooldown",
          honoured > guessed and honoured > 250, f"{honoured}s vs {guessed}s")

    R.book._data.clear()
    R.book.record_failure("x", "m", R.QUOTA, "spent", retry_after=5)
    check("a short Retry-After shortens a long default too",
          R.book.get("x", "m").rest_remaining <= 6,
          f"{R.book.get('x', 'm').rest_remaining}s")

    R.book._data.clear()
    R.book.record_failure("x", "m", R.RATE_LIMIT, "429", retry_after=999999)
    check("an absurd Retry-After is capped",
          R.book.get("x", "m").rest_remaining <= R.MAX_COOLDOWN)


def _api() -> None:
    from fastapi.testclient import TestClient
    from aegis.server import app

    with TestClient(app) as client:
        r = client.get("/api/routes")
        check("GET /api/routes", r.status_code == 200 and "routes" in r.json())

        r = client.post("/api/routes", json={
            "key": "freetest", "label": "Free test", "allow_paid": False,
            "candidates": [{"provider": "openai", "model": "gpt-4o"},
                           {"provider": "ollama", "model": "qwen2.5:7b"}]})
        check("a route saves", r.status_code == 200)
        route = next(x for x in client.get("/api/routes").json()["routes"]
                     if x["key"] == "freetest")
        check("a paid model is stripped from a free route server-side",
              [c["model"] for c in route["candidates"]] == ["qwen2.5:7b"],
              str([c["model"] for c in route["candidates"]]))

        r = client.post("/api/routes/default", json={"key": "freetest"})
        check("a default route can be set", r.json()["default_route"] == "freetest")
        check("an unknown default route is refused",
              client.post("/api/routes/default", json={"key": "nope"}).status_code == 404)

        r = client.get("/api/providers")
        check("routes appear as providers for the chat picker",
              any(p["key"] == "route:freetest" for p in r.json()["providers"]))

        r = client.post("/api/routes/health", json={"action": "revive"})
        check("health can be revived over the API", r.status_code == 200)
        check("a bad health action is refused",
              client.post("/api/routes/health", json={"action": "x"}).status_code == 400)

        r = client.get("/api/custom-providers")
        check("GET /api/custom-providers", r.status_code == 200
              and "presets" in r.json())
        r = client.post("/api/custom-providers",
                        json={"key": "x", "label": "X", "base_url": "nope"})
        check("a bad base URL is refused by the API", r.status_code == 400)

        # An unpriced provider: AEGIS assumes paid, and you can correct it.
        r = client.post("/api/custom-providers", json={
            "key": "atria", "label": "Atria",
            "base_url": "https://api.atria-asi.ai/v1",
            "models": "Atria-Dawn-Preview", "has_models_endpoint": False})
        check("a no-/models provider saves over the API", r.status_code == 200)

        r = client.post("/api/routes", json={
            "key": "free2", "label": "Free 2", "allow_paid": False,
            "candidates": [{"provider": "atria", "model": "Atria-Dawn-Preview"}]})
        route = next(x for x in client.get("/api/routes").json()["routes"]
                     if x["key"] == "free2")
        check("an unpriced model is assumed paid and kept off a free route",
              route["candidates"] == [], str(route["candidates"]))

        r = client.post("/api/routes/free-tag", json={
            "provider": "atria", "model": "Atria-Dawn-Preview", "free": True})
        check("a model can be marked free by hand", r.json()["free"] is True)
        check("...and is then no longer treated as paid",
              r.json()["is_paid"] is False)

        r = client.post("/api/routes", json={
            "key": "free2", "label": "Free 2", "allow_paid": False,
            "candidates": [{"provider": "atria", "model": "Atria-Dawn-Preview"}]})
        route = next(x for x in client.get("/api/routes").json()["routes"]
                     if x["key"] == "free2")
        check("once tagged free it survives on a free route",
              [c["model"] for c in route["candidates"]] == ["Atria-Dawn-Preview"],
              str(route["candidates"]))

        check("free-tag needs both a provider and a model",
              client.post("/api/routes/free-tag",
                          json={"provider": "x"}).status_code == 400)

        client.delete("/api/routes/free2")
        client.delete("/api/custom-providers/atria")

        check("a route can be deleted",
              client.delete("/api/routes/freetest").json()["ok"] is True)


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("error classification")
    _classify()
    section("model health and cooldowns")
    _health()
    section("route planning")
    _planning()
    section("failover behaviour")
    asyncio.run(_failover())
    section("custom providers")
    _custom()
    section("providers without a /models endpoint")
    asyncio.run(_no_models_endpoint())
    section("catalogues that publish no prices")
    asyncio.run(_unpriced_catalogue())
    section("whole-account free tiers, and building a route from them")
    asyncio.run(_free_tier_and_autobuild())
    section("a daily allowance rests until midnight")
    _daily_quota_rests_until_midnight()
    section("Retry-After is honoured")
    _retry_after_wins()
    section("routing API")
    _api()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR",
                          tempfile.mkdtemp(prefix="aegis-router-test-"))
    from harness import report
    run_all()
    sys.exit(report())
