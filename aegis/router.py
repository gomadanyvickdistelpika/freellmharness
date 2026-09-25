"""Model routing: an ordered list of models, and what to do when one dies.

A route is a list of (provider, model) candidates in the order you want them
tried. AEGIS always uses the highest one that is currently healthy, and drops
down the list when one is out of credit, rate-limited or failing.

The distinction that makes this useful rather than annoying:

    FAIL OVER    quota exhausted, rate limited, server error, timeout,
                 model no longer exists - another model genuinely fixes these.
    STOP         bad request, bad API key, content too long - every other model
                 fails the same way, so burning through the list just wastes
                 time and hides the real problem.

Health is per (provider, model), persisted, and time-based: a model that hits
its daily cap is put on a long cooldown; a rate-limited one on a short one,
honouring Retry-After when the provider sends it.

Free and paid routes never mix. A route marked free cannot run a paid model
even when every free one is exhausted - it stops and says so. That is
deliberate: the alternative is a test session quietly spending real money.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict, field
from typing import Any

from .config import settings

# --- error kinds ------------------------------------------------------------
QUOTA = "quota"              # out of credit, daily cap reached
RATE_LIMIT = "rate_limit"
SERVER = "server"            # 5xx
TIMEOUT = "timeout"          # network, connection, read timeout
NOT_FOUND = "not_found"      # model gone
AUTH = "auth"                # bad or missing key
BAD_REQUEST = "bad_request"  # our request is wrong
# v2: a 400 that is really about *this model* - it cannot take tools, cannot
# see images, or its context window is too small. Every other model does not
# fail the same way, so this one fails over (and the gap is remembered).
CAPABILITY = "capability"
EMPTY = "empty"              # the model answered with nothing at all
UNKNOWN = "unknown"

# Kinds where another model is a real fix.
FAILOVER_KINDS = {QUOTA, RATE_LIMIT, SERVER, TIMEOUT, NOT_FOUND, CAPABILITY,
                  EMPTY}

# How long to rest a model after each kind of failure, in seconds.
COOLDOWN = {
    QUOTA: 30 * 60,
    RATE_LIMIT: 60,
    SERVER: 30,
    TIMEOUT: 20,
    NOT_FOUND: 24 * 60 * 60,
    AUTH: 10 * 60,
    UNKNOWN: 45,
    BAD_REQUEST: 0,
    CAPABILITY: 0,           # not rested - the gap is recorded in model_caps
    EMPTY: 120,
}
MAX_COOLDOWN = 6 * 60 * 60
HEALTH_SAVE_EVERY = 5.0      # seconds; health changes on every request


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_QUOTA_HINTS = re.compile(
    r"(insufficient[_ ]?(quota|credit|balance)|out of credit|no credit|"
    r"payment required|billing|quota exceeded|exceeded your current quota|"
    r"credit balance is too low|daily limit|free[- ]tier limit|"
    r"usage limit|spending limit|per[- ]day|free-models-per|"
    r"tokens? per day|requests? per day|allowance)", re.IGNORECASE)

_RATE_HINTS = re.compile(
    r"(rate[_ ]?limit|too many requests|slow down|overloaded|"
    r"capacity|try again (later|in a))", re.IGNORECASE)

_NOTFOUND_HINTS = re.compile(
    r"(model[_ ]?not[_ ]?found|no such model|unknown model|"
    r"does not exist|is not available|deprecated)", re.IGNORECASE)

# Model-specific refusals that look like a bad request but are not ours.
_CAP_TOOLS = re.compile(
    r"(support(s|ed)? (for )?(tool|function)|tool[_ ]?(use|calling|choice)s? "
    r"(is |are )?not (supported|enabled|available)|does not support (tools|"
    r"function)|no endpoints found that support tool|tools? (is|are) not "
    r"supported|function calling is not)", re.IGNORECASE)
_CAP_VISION = re.compile(
    r"(image[s]? (input )?(is |are )?not supported|does not support (image|"
    r"vision|multimodal)|no endpoints found that support image|vision is not|"
    r"image_url is not supported|only text (content|input)|not a (vision|"
    r"multimodal) model)", re.IGNORECASE)
_CAP_CONTEXT = re.compile(
    r"(context[_ ]?(length|window)|maximum context|too many tokens|"
    r"prompt is too long|input is too long|reduce the length|"
    r"exceeds the (model'?s? )?(max|maximum|limit)|request too large|"
    r"token limit)", re.IGNORECASE)


def capability_gap(text: str) -> str:
    """'tools' | 'vision' | 'context' | '' - what this model could not do."""
    text = text or ""
    if _CAP_TOOLS.search(text):
        return "tools"
    if _CAP_VISION.search(text):
        return "vision"
    if _CAP_CONTEXT.search(text):
        return "context"
    return ""


_TIMEOUT_HINTS = re.compile(
    r"(timeout|timed out|connect(ion)?[_ ]?(error|reset|refused|aborted)|"
    r"readtimeout|connecterror|remoteprotocolerror|incomplete)", re.IGNORECASE)

# A daily allowance does not come back in half an hour - it comes back at
# midnight. xKiro's 500k tokens/day is the shape this is for.
_DAILY_HINTS = re.compile(
    r"(daily|per[- ]day|today|24[- ]hour|resets? (at|tomorrow|daily))",
    re.IGNORECASE)


def seconds_until_midnight(now: float | None = None) -> float:
    """Time to the next local midnight, plus a minute so we do not race it."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(now) if now else _dt.datetime.now()
    tomorrow = (moment + _dt.timedelta(days=1)).replace(
        hour=0, minute=1, second=0, microsecond=0)
    return max(60.0, (tomorrow - moment).total_seconds())


def classify(status: int | None = None, text: str = "") -> str:
    """What kind of failure is this? Status code wins; text is the fallback.

    429 is deliberately checked against the body as well: several providers
    return 429 for 'you have used your free allowance for today', which is a
    quota problem wearing a rate-limit hat and deserves a much longer rest.
    """
    text = text or ""

    if status == 401 or status == 403:
        return AUTH
    if status == 402:
        return QUOTA
    if status == 413:
        return CAPABILITY
    if status in (400, 404, 422) and capability_gap(text):
        # OpenRouter says 404 "No endpoints found that support tool use";
        # most others say 400. Either way it is this model, not the request.
        return CAPABILITY
    if status == 404:
        return NOT_FOUND
    if status == 429:
        return QUOTA if _QUOTA_HINTS.search(text) else RATE_LIMIT
    if status is not None and 500 <= status < 600:
        return SERVER
    if status == 400 or status == 422:
        # A 400 that mentions credit is still a money problem, not our bug.
        return QUOTA if _QUOTA_HINTS.search(text) else BAD_REQUEST
    if status is not None and 200 <= status < 400:
        return UNKNOWN

    if _QUOTA_HINTS.search(text):
        return QUOTA
    if capability_gap(text):
        return CAPABILITY
    if _NOTFOUND_HINTS.search(text):
        return NOT_FOUND
    if _RATE_HINTS.search(text):
        return RATE_LIMIT
    if _TIMEOUT_HINTS.search(text):
        return TIMEOUT
    return UNKNOWN


def should_failover(kind: str) -> bool:
    return kind in FAILOVER_KINDS


def retry_after_seconds(text: str) -> float | None:
    """Pull a Retry-After hint out of an error body when the provider sends one."""
    match = re.search(r"retry[- _]?after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)",
                      text or "", re.IGNORECASE)
    if match:
        try:
            return min(float(match.group(1)), MAX_COOLDOWN)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@dataclass
class Health:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_kind: str = ""
    last_used: float = 0.0
    tps: float = 0.0             # EWMA tokens/sec, measured
    samples: int = 0

    @property
    def resting(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def rest_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["resting"] = self.resting
        d["rest_remaining"] = self.rest_remaining
        d["tps"] = round(self.tps, 1)
        return d


class HealthBook:
    """Per-(provider, model) health, persisted so cooldowns survive a restart."""

    def __init__(self) -> None:
        self._data: dict[str, Health] = {}
        self._loaded = False
        self._last_save = 0.0

    def _load(self) -> None:
        if self._loaded:
            return
        raw = settings.get("model_health") or {}
        for key, value in raw.items():
            if isinstance(value, dict):
                try:
                    self._data[key] = Health(**{
                        k: v for k, v in value.items()
                        if k in Health.__dataclass_fields__})
                except TypeError:
                    continue
        self._loaded = True

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < HEALTH_SAVE_EVERY:
            return
        self._last_save = now
        settings.set("model_health",
                     {k: asdict(v) for k, v in self._data.items()})

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}::{model}"

    def get(self, provider: str, model: str) -> Health:
        self._load()
        return self._data.setdefault(self.key(provider, model), Health())

    def peek(self, provider: str, model: str) -> Health:
        """Like get(), but never creates an entry - for read-only checks."""
        self._load()
        return self._data.get(self.key(provider, model)) or Health()

    def record_success(self, provider: str, model: str, *,
                       output_tokens: int = 0, seconds: float = 0.0) -> None:
        health = self.get(provider, model)
        health.successes += 1
        health.consecutive_failures = 0
        health.cooldown_until = 0.0
        health.last_error = ""
        health.last_kind = ""
        health.last_used = time.time()
        if output_tokens > 0 and seconds > 0.25:
            observed = output_tokens / seconds
            health.tps = (observed if health.samples == 0
                          else health.tps * 0.7 + observed * 0.3)
            health.samples += 1
        self._save()

    def record_failure(self, provider: str, model: str, kind: str,
                       detail: str = "", retry_after: float | None = None) -> Health:
        health = self.get(provider, model)
        health.failures += 1
        health.consecutive_failures += 1
        health.last_error = (detail or "")[:400]
        health.last_kind = kind
        health.last_used = time.time()

        base = COOLDOWN.get(kind, COOLDOWN[UNKNOWN])
        # A Retry-After header is the provider telling us exactly when to come
        # back. Prefer it over anything we would guess.
        if retry_after is not None:
            return self._rest(health, min(max(retry_after, 1.0), MAX_COOLDOWN))
        # A spent daily allowance returns at midnight, not in half an hour.
        # Retrying it every 30 minutes all afternoon just wastes requests.
        if kind == QUOTA and _DAILY_HINTS.search(detail or ""):
            health.last_kind = f"{QUOTA} (daily)"
            return self._rest(health, seconds_until_midnight())
        if hinted := retry_after_seconds(detail):
            base = max(base, hinted)
        if base:
            # Back off on repeats, but never past the ceiling.
            factor = min(2 ** max(0, health.consecutive_failures - 1), 8)
            return self._rest(health, min(base * factor, MAX_COOLDOWN))
        self._save(force=True)
        return health

    def _rest(self, health: Health, seconds: float) -> Health:
        health.cooldown_until = time.time() + seconds
        self._save(force=True)
        return health

    def clear(self, provider: str = "", model: str = "") -> int:
        self._load()
        if provider:
            key = self.key(provider, model)
            existed = key in self._data
            self._data.pop(key, None)
            self._save(force=True)
            return int(existed)
        count = len(self._data)
        self._data.clear()
        self._save(force=True)
        return count

    def revive_all(self) -> int:
        """Lift every cooldown without losing the speed measurements."""
        self._load()
        count = 0
        for health in self._data.values():
            if health.resting:
                health.cooldown_until = 0.0
                health.consecutive_failures = 0
                count += 1
        self._save(force=True)
        return count

    def all(self) -> dict[str, dict[str, Any]]:
        self._load()
        return {k: v.to_dict() for k, v in self._data.items()}


book = HealthBook()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    provider: str
    model: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Route:
    key: str
    label: str
    candidates: list[Candidate] = field(default_factory=list)
    allow_paid: bool = True
    description: str = ""
    # v2: reorder by what the request needs (code, images, long context,
    # quick answers) instead of always walking the list top to bottom.
    smart: bool = True
    # v2: when every model here is spent, carry on down this other route.
    # Empty by default: a free route never reaches a paid one unless you
    # wire it up yourself, and the switch is announced in the chat.
    then_route: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label,
                "allow_paid": self.allow_paid, "description": self.description,
                "smart": self.smart, "then_route": self.then_route,
                "candidates": [c.to_dict() for c in self.candidates]}


DEFAULT_ROUTES: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto (free, smart)",
        "allow_paid": False,
        "smart": True,
        "description": ("Every free model you have, picked per request: coders "
                        "for code, vision models for images, long-context "
                        "models for big documents, fast ones for quick "
                        "questions. Refilled by 'Build my free route'."),
        "candidates": [],
    },
    "test": {
        "label": "Test (free only)",
        "allow_paid": False,
        "description": ("Free models for building and testing. If every one is "
                        "exhausted this route stops rather than spending money."),
        "candidates": [],
    },
    "production": {
        "label": "Production",
        "allow_paid": True,
        "description": "Paid models for work that matters.",
        "candidates": [],
    },
}


def routes() -> dict[str, Route]:
    stored = settings.get("routes")
    if not stored:
        stored = DEFAULT_ROUTES
        settings.set("routes", stored)
    elif "auto" not in stored and not settings.get("auto_route_seeded"):
        # Upgrading from v1: add the smart free route once, and never again
        # if you delete it.
        stored = {"auto": DEFAULT_ROUTES["auto"], **stored}
        settings.update({"routes": stored, "auto_route_seeded": True})
    out: dict[str, Route] = {}
    for key, value in stored.items():
        if not isinstance(value, dict):
            continue
        out[key] = Route(
            key=key,
            label=value.get("label", key),
            allow_paid=bool(value.get("allow_paid", True)),
            description=value.get("description", ""),
            smart=bool(value.get("smart", True)),
            then_route=str(value.get("then_route", "") or ""),
            candidates=[Candidate(provider=c.get("provider", ""),
                                  model=c.get("model", ""),
                                  note=c.get("note", ""))
                        for c in value.get("candidates", [])
                        if isinstance(c, dict) and c.get("provider")],
        )
    return out


def get_route(key: str) -> Route | None:
    return routes().get(key)


def save_route(route: Route) -> None:
    stored = dict(settings.get("routes") or {})
    stored[route.key] = route.to_dict()
    stored[route.key].pop("key", None)
    settings.set("routes", stored)


def delete_route(key: str) -> bool:
    stored = dict(settings.get("routes") or {})
    existed = key in stored
    stored.pop(key, None)
    settings.set("routes", stored)
    return existed


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def is_paid(provider_key: str, model: str) -> bool:
    """Is this candidate going to cost money?

    Free, in order of how confident we are:
      * a local engine - it is your own machine;
      * a provider you marked free-tier, which is how xKiro, 9Router and an
        Atria preview key actually work: the allowance is on the plan, not on
        any one model;
      * a model you tagged free by hand;
      * an id ending ':free', which is OpenRouter's own marker.

    Everything else is assumed to cost. That is the safe direction to be wrong
    in: the other way spends your money.
    """
    from .providers import custom, get as get_provider

    if provider_key in ("ollama", "lmstudio", "llamacpp"):
        return False
    if custom.is_free_tier(provider_key):
        return False
    if model.endswith(":free"):
        return False
    if f"{provider_key}::{model}" in (settings.get("free_models") or []):
        return False
    provider = get_provider(provider_key)
    if provider and provider.kind == "local":
        return False
    return True


@dataclass
class Plan:
    """What the router intends to try, and what it has ruled out."""
    usable: list[Candidate] = field(default_factory=list)
    resting: list[tuple[Candidate, Health]] = field(default_factory=list)
    excluded: list[tuple[Candidate, str]] = field(default_factory=list)
    # Not failed - just out of allowance for now. Kept separate from resting
    # because nothing has actually gone wrong with these.
    over_budget: list[tuple[Candidate, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.usable)

    def explain(self) -> str:
        bits = []
        for cand, health in self.resting:
            bits.append(f"{cand.provider}/{cand.model}: {health.last_kind or 'resting'}"
                        f", {health.rest_remaining}s left")
        for cand, verdict in self.over_budget:
            bits.append(f"{cand.provider}/{cand.model}: {verdict.why}"
                        f", {int(verdict.retry_in)}s left")
        for cand, why in self.excluded:
            bits.append(f"{cand.provider}/{cand.model}: {why}")
        return "; ".join(bits) or "no candidates configured"


# ---------------------------------------------------------------------------
# What each model can do - read from catalogues, learned from refusals
# ---------------------------------------------------------------------------

def caps(provider: str, model: str) -> dict[str, Any]:
    """{'tools': bool|None, 'vision': bool|None, 'ctx': int|None}. None = unknown."""
    stored = (settings.get("model_caps") or {}).get(f"{provider}::{model}") or {}
    return {"tools": stored.get("tools"), "vision": stored.get("vision"),
            "ctx": stored.get("ctx"), "source": stored.get("source", "")}


def learn_caps(provider: str, model: str, *, source: str = "learned",
               **found: Any) -> None:
    """Record what a model can or cannot do. A learned refusal beats a catalogue."""
    table = dict(settings.get("model_caps") or {})
    key = f"{provider}::{model}"
    entry = dict(table.get(key) or {})
    for name in ("tools", "vision", "ctx"):
        if name in found and found[name] is not None:
            if entry.get("source") == "learned" and source == "catalogue" \
                    and name in entry:
                continue        # the model told us itself; keep that
            entry[name] = found[name]
    entry["source"] = source if source == "learned" else entry.get("source", source)
    table[key] = entry
    settings.set("model_caps", table)


def learn_caps_bulk(provider: str, rows: list[dict[str, Any]]) -> int:
    """Store catalogue capabilities for many models in one settings write."""
    table = dict(settings.get("model_caps") or {})
    n = 0
    for row in rows:
        mid = row.get("id")
        if not mid:
            continue
        key = f"{provider}::{mid}"
        entry = dict(table.get(key) or {})
        learned = entry.get("source") == "learned"
        for name in ("tools", "vision", "ctx"):
            value = row.get(name)
            if value is None or (learned and name in entry):
                continue
            entry[name] = value
        entry.setdefault("source", "catalogue")
        table[key] = entry
        n += 1
    settings.set("model_caps", table)
    return n


def learn_from_refusal(provider: str, model: str, detail: str,
                       est_tokens: int = 0) -> str:
    gap = capability_gap(detail)
    if gap == "tools":
        learn_caps(provider, model, tools=False)
    elif gap == "vision":
        learn_caps(provider, model, vision=False)
    elif gap == "context" and est_tokens:
        current = caps(provider, model).get("ctx")
        limit = int(est_tokens * 0.9)
        if not current or limit < current:
            learn_caps(provider, model, ctx=max(2048, limit))
    return gap


def block_provider(provider: str, seconds: float, why: str) -> None:
    """Rest every model on one provider - a rejected key is rejected for all."""
    health = book.get(provider, "*")
    health.last_kind = AUTH
    health.last_error = why[:400]
    health.failures += 1
    book._rest(health, seconds)


@dataclass
class Needs:
    """What a request needs from a model."""
    tools: bool = False
    images: bool = False
    tokens: int = 0


def plan(route: Route, *, require_tools: bool = False,
         needs: Needs | None = None) -> Plan:
    """Work out, in order, which candidates can be tried right now."""
    from . import budget
    from .providers import registry

    needs = needs or Needs(tools=require_tools)
    if require_tools:
        needs.tools = True
    available = registry()
    result = Plan()

    for cand in route.candidates:
        provider = available.get(cand.provider)
        if provider is None:
            result.excluded.append((cand, "provider not configured"))
            continue
        if needs.tools and not provider.supports_tools:
            result.excluded.append((cand, "cannot use tools"))
            continue
        known = caps(cand.provider, cand.model)
        if needs.tools and known["tools"] is False:
            result.excluded.append((cand, "model cannot use tools"))
            continue
        if needs.images and known["vision"] is False:
            result.excluded.append((cand, "model cannot see images"))
            continue
        if needs.tokens and known["ctx"] and needs.tokens > int(known["ctx"]) * 0.95:
            result.excluded.append((cand, f"context too small ({known['ctx']:,})"))
            continue
        if not route.allow_paid and is_paid(cand.provider, cand.model):
            result.excluded.append((cand, "paid model on a free-only route"))
            continue
        blocked = book.peek(cand.provider, "*")
        if blocked.resting:
            result.resting.append((cand, blocked))
            continue
        health = book.get(cand.provider, cand.model)
        if health.resting:
            result.resting.append((cand, health))
            continue
        # Checked after health so a model that has actually failed is reported
        # as failed rather than as merely busy.
        verdict = budget.ledger.check(cand.provider, cand.model)
        if not verdict.ok:
            result.over_budget.append((cand, verdict))
            continue
        result.usable.append(cand)

    return result
