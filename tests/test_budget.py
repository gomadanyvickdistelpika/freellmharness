"""Budgets: stepping off a model before it refuses, rather than after.

The router's failover already proves AEGIS recovers from a 429. What these
tests prove is the thing that makes a free-tier setup usable rather than merely
survivable: AEGIS counts what it has spent, reads what the provider says is
left, and moves to another model *without a 429 and without asking*.

The last section is the one that matters most to the actual use: xKiro's daily
allowance runs out mid-session and the answer still arrives, from OpenRouter,
with no prompt and no error shown to the user.
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
    """Replays a fixed script and remembers it was called."""
    kind = "api"
    supports_tools = True
    supports_images = True
    blurb = ""
    needs = ""

    def __init__(self, key: str, script: list[dict[str, Any]]) -> None:
        self.key = key
        self.label = key
        self.script = script
        self.calls: list[str] = []

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["m"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(model)
        for event in self.script:
            yield event


class FakeHeaders:
    """httpx headers are case-insensitive; a plain dict is not."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name.lower(), default)


def fresh() -> None:
    """Start each section from an empty ledger and no stored limits."""
    from aegis import budget
    from aegis.config import settings

    settings.update({"model_budgets": {}, "learned_budgets": {},
                     "budget_usage": {}})
    budget.ledger._data.clear()
    budget.ledger._loaded = True
    budget.ledger._last_save = 0.0


# ---------------------------------------------------------------------------
# 1. Counting
# ---------------------------------------------------------------------------

def _counting() -> None:
    from aegis import budget

    fresh()

    check("a model with no limit set is always allowed",
          budget.ledger.check("p", "m").ok is True)

    budget.set_limits("p", "m", rpd=3)
    for _ in range(2):
        budget.ledger.record_request("p", "m")
    check("under the daily cap it stays usable",
          budget.ledger.check("p", "m").ok is True)

    budget.ledger.record_request("p", "m")
    verdict = budget.ledger.check("p", "m")
    check("the third request spends a cap of three", verdict.ok is False)
    check("...and it says so in words a person can read",
          "daily request budget" in verdict.why, verdict.why)
    check("...and it comes back at midnight, not in a minute",
          verdict.retry_in > 60, f"{verdict.retry_in:.0f}s")

    check("another model on the same provider is untouched",
          budget.ledger.check("p", "other").ok is True)


def _minute_window() -> None:
    from aegis import budget

    fresh()
    budget.set_limits("p", "m", rpm=2)
    now = time.time()

    budget.ledger.record_request("p", "m", now=now - 59)
    budget.ledger.record_request("p", "m", now=now - 58)
    verdict = budget.ledger.check("p", "m", now=now)
    check("two requests in the last minute fill a cap of two",
          verdict.ok is False, verdict.why)
    check("it waits only until the oldest one ages out",
          0 < verdict.retry_in <= 2, f"{verdict.retry_in:.1f}s")

    # Two seconds later the first request is outside the window.
    check("a slot frees up as the window slides",
          budget.ledger.check("p", "m", now=now + 2).ok is True)


def _tokens_and_day() -> None:
    from aegis import budget

    fresh()
    budget.set_limits("p", "m", tpd=1000)
    budget.ledger.record_tokens("p", "m", 400)
    budget.ledger.record_tokens("p", "m", 400)
    check("token spend accumulates across calls",
          budget.ledger.usage("p", "m").tokens == 800)
    check("under the token cap it stays usable",
          budget.ledger.check("p", "m").ok is True)

    budget.ledger.record_tokens("p", "m", 400)
    verdict = budget.ledger.check("p", "m")
    check("passing the daily token cap stops it", verdict.ok is False)
    check("...and the numbers are in the message",
          "1,200" in verdict.why and "1,000" in verdict.why, verdict.why)

    # Yesterday's spend must not count against today.
    budget.ledger.usage("p", "m").day = "2000-01-01"
    check("the day bucket resets on a new local day",
          budget.ledger.check("p", "m").ok is True)
    check("...and the counters actually went back to zero",
          budget.ledger.usage("p", "m").tokens == 0)


def _limit_precedence() -> None:
    from aegis import budget
    from aegis.config import settings

    fresh()
    settings.set("learned_budgets",
                 {"p::m": {"limits": {"rpd": 10}, "from": "response headers"}})
    check("a limit implied by headers is used when you have set none",
          budget.limits_for("p", "m").rpd == 10)
    check("...and is labelled as inferred, not as yours",
          budget.limits_for("p", "m").source == "inferred")

    budget.set_limits("p", "*", rpd=50)
    check("a provider-wide number you set beats an inferred one",
          budget.limits_for("p", "m").rpd == 50)

    budget.set_limits("p", "m", rpd=5)
    check("a number set on the model itself beats the provider default",
          budget.limits_for("p", "m").rpd == 5)
    check("...and is labelled as yours",
          budget.limits_for("p", "m").source == "you")

    budget.set_limits("p", "m", rpd=0)
    check("setting it back to zero removes the limit entirely",
          budget.limits_for("p", "m").rpd == 50, "should fall back to p::*")


# ---------------------------------------------------------------------------
# 2. Listening to the provider
# ---------------------------------------------------------------------------

def _durations() -> None:
    from aegis import budget

    cases = [("58s", 58.0), ("1m30s", 90.0), ("2h", 7200.0),
             ("750ms", 0.75), ("42", 42.0), ("7.66s", 7.66)]
    for raw, expected in cases:
        got = budget.parse_duration(raw)
        check(f"'{raw}' parses as {expected}s",
              got is not None and abs(got - expected) < 0.01, str(got))
    check("nonsense parses as nothing",
          budget.parse_duration("soon") is None)
    check("an empty value parses as nothing",
          budget.parse_duration("") is None)


def _headers() -> None:
    from aegis import budget
    from aegis.config import settings

    fresh()
    seen = budget.note_headers("p", "m", FakeHeaders({
        "x-ratelimit-limit-requests": "14400",
        "x-ratelimit-remaining-requests": "13500",
        "x-ratelimit-reset-requests": "2h30m",
    }))
    check("what is left is read off the response",
          seen["requests"]["remaining"] == 13500.0, str(seen))
    check("plenty remaining leaves the model usable",
          budget.ledger.check("p", "m").ok is True)
    learned = settings.get("learned_budgets")["p::m"]["limits"]
    check("a long reset interval means the limit is a daily one",
          learned.get("rpd") == 14400, str(learned))

    fresh()
    budget.note_headers("q", "m", FakeHeaders({
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "29",
        "x-ratelimit-reset-requests": "2s",
    }))
    learned = settings.get("learned_budgets")["q::m"]["limits"]
    check("a short reset interval means the limit is a per-minute one",
          learned.get("rpm") == 30, str(learned))

    # The case this exists for: nothing left, and the provider said when.
    fresh()
    budget.note_headers("p", "m", FakeHeaders({
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "45s",
    }))
    verdict = budget.ledger.check("p", "m")
    check("nothing left means the model is stepped over", verdict.ok is False)
    check("...for as long as the provider asked, not a guess",
          40 <= verdict.retry_in <= 46, f"{verdict.retry_in:.0f}s")
    check("...and the reason names the provider, not AEGIS",
          "left on this key" in verdict.why, verdict.why)

    fresh()
    check("headers that say nothing useful change nothing",
          budget.note_headers("p", "m", FakeHeaders({"content-type": "x"})) == {})
    check("...and no headers at all is not an error",
          budget.note_headers("p", "m", None) == {})


# ---------------------------------------------------------------------------
# 3. What the router does with it
# ---------------------------------------------------------------------------

def _planning() -> None:
    from aegis import budget, router as R
    from aegis.config import settings

    fresh()
    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    from test_router import patch_registry, restore_registry

    spent = FakeProvider("a", [])
    spare = FakeProvider("b", [])
    original, module = patch_registry({"a": spent, "b": spare})
    try:
        budget.set_limits("a", "m1", rpd=1)
        budget.ledger.record_request("a", "m1")

        route = R.Route(key="r", label="R", candidates=[
            R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        attempt = R.plan(route)

        check("a model over its budget is not offered",
              [c.model for c in attempt.usable] == ["m2"],
              str([c.model for c in attempt.usable]))
        check("it is reported as over budget, not as failed",
              len(attempt.over_budget) == 1 and not attempt.resting)
        check("the route is still ready because another model is free",
              attempt.ok is True)
        check("the explanation says which allowance ran out",
              "daily request budget" in attempt.explain(), attempt.explain())
    finally:
        restore_registry(original, module)


async def _silent_switch() -> None:
    """The real scenario: the daily allowance runs out and nobody is asked."""
    from aegis import budget, router as R
    from aegis.config import settings
    from aegis.providers.routed import RoutedProvider
    from test_router import patch_registry, restore_registry

    fresh()
    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    xkiro = FakeProvider("xkiro", [{"type": "delta", "text": "from xkiro"},
                                   {"type": "done"}])
    openrouter = FakeProvider("openrouter", [
        {"type": "delta", "text": "from openrouter"},
        {"type": "usage", "input": 10, "output": 20},
        {"type": "done"}])

    # Marked free-tier the way you would mark it in the UI: xKiro publishes no
    # per-model pricing, so without this the free-only route treats it as paid
    # and never tries it at all.
    from aegis.providers import custom
    custom.save("xkiro", "xKiro", "https://api.xkiro.com/v1", free_tier=True)

    original, module = patch_registry({"xkiro": xkiro, "openrouter": openrouter})
    try:
        check("a free-tier provider is not treated as paid",
              R.is_paid("xkiro", "openai/gpt-5.6-sol") is False)
        # One request a day on xKiro, so the second turn has to move.
        budget.set_limits("xkiro", "*", rpd=1)
        route = R.Route(key="test", label="Test (free only)", allow_paid=False,
                        candidates=[R.Candidate("xkiro", "openai/gpt-5.6-sol"),
                                    R.Candidate("openrouter", "z-ai/glm:free")])

        first = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "one"}], "")]
        text = "".join(e.get("text", "") for e in first if e["type"] == "delta")
        check("the first turn goes to the preferred provider",
              text == "from xkiro", text)

        second = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "two"}], "")]
        text = "".join(e.get("text", "") for e in second if e["type"] == "delta")
        kinds = [e["type"] for e in second]

        check("the next turn answers from the other provider instead",
              text == "from openrouter", text)
        check("the user is shown no error at all",
              "error" not in kinds, str(kinds))
        check("nothing is asked and no approval is waited for",
              "ask" not in kinds and "approval" not in kinds, str(kinds))
        check("the exhausted provider is not called again",
              xkiro.calls == ["openai/gpt-5.6-sol"], str(xkiro.calls))
        check("no 429 was needed to work this out",
              R.book.get("xkiro", "openai/gpt-5.6-sol").failures == 0)
        check("the switch is silent - no 'switched to' notice on a fresh turn",
              sum(1 for e in second if e["type"] == "notice") == 0, str(kinds))
        check("tokens spent on the second provider are counted",
              budget.ledger.usage("openrouter", "z-ai/glm:free").tokens == 30,
              str(budget.ledger.usage("openrouter", "z-ai/glm:free").tokens))
    finally:
        restore_registry(original, module)
        custom.remove("xkiro")


async def _everything_spent() -> None:
    """When every free model is out, say so plainly - do not reach for a paid one."""
    from aegis import budget, router as R
    from aegis.config import settings
    from aegis.providers.routed import RoutedProvider
    from test_router import patch_registry, restore_registry

    fresh()
    settings.set("model_health", {})
    R.book._data.clear()
    R.book._loaded = True

    a = FakeProvider("a", [{"type": "delta", "text": "no"}, {"type": "done"}])
    b = FakeProvider("b", [{"type": "delta", "text": "no"}, {"type": "done"}])
    original, module = patch_registry({"a": a, "b": b})
    try:
        settings.set("free_models", ["a::m1", "b::m2"])
        budget.set_limits("a", "*", rpd=1)
        budget.set_limits("b", "*", rpd=1)
        budget.ledger.record_request("a", "m1")
        budget.ledger.record_request("b", "m2")

        route = R.Route(key="test", label="Test (free only)", allow_paid=False,
                        candidates=[R.Candidate("a", "m1"), R.Candidate("b", "m2")])
        events = [e async for e in RoutedProvider(route).chat(
            [{"role": "user", "content": "hi"}], "")]

        check("with everything spent it stops rather than pretending",
              any(e["type"] == "error" for e in events))
        text = " ".join(e.get("text", "") for e in events if e["type"] == "error")
        check("the message says it is an allowance problem, not a fault",
              "allowance" in text.lower(), text[:120])
        check("...and it does not offer to spend money on a free route",
              "free-only" in text, text[:200])
        check("neither model was called",
              a.calls == [] and b.calls == [], f"{a.calls} {b.calls}")
    finally:
        restore_registry(original, module)


# ---------------------------------------------------------------------------
# 4. The API
# ---------------------------------------------------------------------------

def _api() -> None:
    from fastapi.testclient import TestClient

    from aegis import budget
    from aegis.server import app

    fresh()
    with TestClient(app) as client:
        r = client.post("/api/routes/budget",
                        json={"provider": "xkiro", "model": "*", "rpd": 500})
        check("a provider-wide budget can be set over the API",
              r.json()["limits"]["rpd"] == 500, r.text[:160])

        check("a budget needs a provider and a model",
              client.post("/api/routes/budget",
                          json={"rpd": 5}).status_code == 400)

        budget.ledger.record_request("xkiro", "openai/gpt-5.6-sol")
        body = client.get("/api/routes/budget").json()
        check("spend shows up in the snapshot",
              body["usage"]["xkiro::openai/gpt-5.6-sol"]["requests"] == 1,
              str(body["usage"])[:200])
        check("the snapshot carries the limit that applies",
              body["usage"]["xkiro::openai/gpt-5.6-sol"]["limits"]["rpd"] == 500)
        check("the fields are labelled for a human",
              body["fields"]["rpm"] == "requests/minute", str(body["fields"]))

        r = client.post("/api/routes/budget",
                        json={"action": "reset", "provider": "xkiro",
                              "model": "openai/gpt-5.6-sol"})
        check("spend can be reset by hand", r.json()["cleared"] == 1, r.text[:160])
        check("...and the counter really is back to zero",
              budget.ledger.usage("xkiro", "openai/gpt-5.6-sol").requests == 0)

        check("the routes listing carries the budget with it",
              "budget" in client.get("/api/routes").json())


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("budget: counting what has been spent")
    _counting()
    section("budget: the rolling minute")
    _minute_window()
    section("budget: tokens and the daily reset")
    _tokens_and_day()
    section("budget: whose number wins")
    _limit_precedence()
    section("budget: reading reset intervals")
    _durations()
    section("budget: listening to rate-limit headers")
    _headers()
    section("budget: route planning")
    _planning()
    section("budget: switching provider with nothing asked")
    asyncio.run(_silent_switch())
    section("budget: when every free model is spent")
    asyncio.run(_everything_spent())
    section("budget: the API")
    _api()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR",
                          tempfile.mkdtemp(prefix="aegis-budget-test-"))
    from harness import report
    run_all()
    sys.exit(report())
