"""Stay inside a free tier instead of discovering its edge with a 429.

The router already handles a model that has *just* failed: classify, rest it,
drop to the next one. This module handles the step before that - knowing you
are about to run out and stepping off first.

Three things feed the decision, in descending order of how much they can be
trusted:

  1. A hold the provider asked for.  When a response carries
     'x-ratelimit-remaining-requests: 0' and 'x-ratelimit-reset-requests: 58s',
     that is not a guess - the provider has said when to come back. AEGIS
     records it and the route steps over that model until then.

  2. A limit you typed in.  Your plan, your numbers. Counted locally against a
     rolling minute and a local-midnight day, so it works with providers that
     send no headers at all.

  3. A limit inferred from headers.  Recorded for display and used only when
     you have set nothing yourself. The window is inferred from the reset
     interval - short means per-minute, long means per-day - because the
     header names alone do not say, and different providers mean different
     things by the same name.

Nothing here is a table of provider limits. Free tiers change monthly; a
shipped list of them would be wrong by the time you read it.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import settings

SAVE_EVERY = 5.0          # seconds between writes; usage changes every request
MINUTE = 60.0
WILDCARD = "*"

# Fields a limit can set. 0 means "no limit".
FIELDS = ("rpm", "rpd", "tpd")
FIELD_LABEL = {"rpm": "requests/minute",
               "rpd": "requests/day",
               "tpd": "tokens/day"}


def today() -> str:
    """Local date. Free tiers reset on the provider's clock, not on UTC, and
    the user's own clock is a far better approximation of it than UTC is."""
    return _dt.date.today().isoformat()


def seconds_until_midnight(now: float | None = None) -> float:
    moment = _dt.datetime.fromtimestamp(now) if now else _dt.datetime.now()
    tomorrow = (moment + _dt.timedelta(days=1)).replace(
        hour=0, minute=1, second=0, microsecond=0)
    return max(60.0, (tomorrow - moment).total_seconds())


def key_of(provider: str, model: str) -> str:
    return f"{provider}::{model}"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

@dataclass
class Limits:
    rpm: int = 0
    rpd: int = 0
    tpd: int = 0
    source: str = ""          # "" | "you" | "inferred"

    def any(self) -> bool:
        return bool(self.rpm or self.rpd or self.tpd)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(raw: Any, source: str) -> Limits:
    if not isinstance(raw, dict):
        return Limits()
    values: dict[str, Any] = {}
    for name in FIELDS:
        try:
            values[name] = max(0, int(raw.get(name) or 0))
        except (TypeError, ValueError):
            values[name] = 0
    return Limits(**values, source=source)


def set_limits(provider: str, model: str, **values: Any) -> Limits:
    """Record your own numbers. model='*' sets a default for the provider."""
    stored = dict(settings.get("model_budgets") or {})
    entry = dict(stored.get(key_of(provider, model)) or {})
    for name in FIELDS:
        if name in values:
            try:
                entry[name] = max(0, int(values[name] or 0))
            except (TypeError, ValueError):
                entry[name] = 0
    limits = _clean(entry, "you")
    if limits.any():
        stored[key_of(provider, model)] = {n: getattr(limits, n) for n in FIELDS}
    else:
        stored.pop(key_of(provider, model), None)
    settings.set("model_budgets", stored)
    return limits


def limits_for(provider: str, model: str) -> Limits:
    """Your numbers for this model, else for the provider, else what the
    provider's own headers implied. Yours always win."""
    stored = settings.get("model_budgets") or {}
    for candidate in (key_of(provider, model), key_of(provider, WILDCARD)):
        limits = _clean(stored.get(candidate), "you")
        if limits.any():
            return limits
    learned = settings.get("learned_budgets") or {}
    return _clean((learned.get(key_of(provider, model)) or {}).get("limits"),
                  "inferred")


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    day: str = ""
    requests: int = 0
    tokens: int = 0
    minute: list[float] = field(default_factory=list)
    hold_until: float = 0.0
    hold_why: str = ""

    def roll(self, now: float) -> None:
        """Drop anything that has aged out: the day bucket at local midnight,
        the minute window continuously."""
        stamp = today()
        if self.day != stamp:
            self.day, self.requests, self.tokens = stamp, 0, 0
        cutoff = now - MINUTE
        if self.minute and self.minute[0] <= cutoff:
            self.minute = [t for t in self.minute if t > cutoff]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["per_minute"] = len(self.minute)
        out.pop("minute", None)
        return out


@dataclass
class Verdict:
    ok: bool
    why: str = ""
    retry_in: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "why": self.why,
                "retry_in": int(self.retry_in)}


class Ledger:
    """What each model has spent today, persisted so a restart does not hand
    you a fresh allowance you do not actually have."""

    def __init__(self) -> None:
        self._data: dict[str, Usage] = {}
        self._loaded = False
        self._last_save = 0.0

    def _load(self) -> None:
        if self._loaded:
            return
        for key, value in (settings.get("budget_usage") or {}).items():
            if not isinstance(value, dict):
                continue
            self._data[key] = Usage(
                day=str(value.get("day") or ""),
                requests=int(value.get("requests") or 0),
                tokens=int(value.get("tokens") or 0),
                minute=[float(t) for t in (value.get("minute") or [])
                        if isinstance(t, (int, float))],
                hold_until=float(value.get("hold_until") or 0.0),
                hold_why=str(value.get("hold_why") or ""))
        self._loaded = True

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < SAVE_EVERY:
            return
        self._last_save = now
        settings.set("budget_usage",
                     {k: {"day": u.day, "requests": u.requests,
                          "tokens": u.tokens, "minute": u.minute[-200:],
                          "hold_until": u.hold_until, "hold_why": u.hold_why}
                      for k, u in self._data.items()})

    def usage(self, provider: str, model: str, now: float | None = None) -> Usage:
        self._load()
        entry = self._data.setdefault(key_of(provider, model), Usage(day=today()))
        entry.roll(now or time.time())
        return entry

    # -- recording --------------------------------------------------------

    def record_request(self, provider: str, model: str,
                       now: float | None = None) -> None:
        moment = now or time.time()
        entry = self.usage(provider, model, moment)
        entry.requests += 1
        entry.minute.append(moment)
        self._save()

    def record_tokens(self, provider: str, model: str, tokens: int) -> None:
        if tokens <= 0:
            return
        entry = self.usage(provider, model)
        entry.tokens += int(tokens)
        self._save()

    def hold(self, provider: str, model: str, seconds: float, why: str) -> None:
        """Step over this model for a while. Used when a provider says so."""
        entry = self.usage(provider, model)
        until = time.time() + max(0.0, seconds)
        if until > entry.hold_until:
            entry.hold_until, entry.hold_why = until, why
            self._save(force=True)

    def clear(self, provider: str = "", model: str = "") -> int:
        self._load()
        if provider:
            key = key_of(provider, model)
            existed = key in self._data
            self._data.pop(key, None)
            self._save(force=True)
            return int(existed)
        count = len(self._data)
        self._data.clear()
        self._save(force=True)
        return count

    # -- the question the router asks -------------------------------------

    def check(self, provider: str, model: str,
              now: float | None = None) -> Verdict:
        moment = now or time.time()
        entry = self.usage(provider, model, moment)

        if entry.hold_until > moment:
            return Verdict(False, entry.hold_why or "the provider asked us to wait",
                           entry.hold_until - moment)

        limits = limits_for(provider, model)
        if not limits.any():
            return Verdict(True)

        if limits.rpd and entry.requests >= limits.rpd:
            return Verdict(False,
                           f"daily request budget spent "
                           f"({entry.requests}/{limits.rpd})",
                           seconds_until_midnight(moment))
        if limits.tpd and entry.tokens >= limits.tpd:
            return Verdict(False,
                           f"daily token budget spent "
                           f"({entry.tokens:,}/{limits.tpd:,})",
                           seconds_until_midnight(moment))
        if limits.rpm and len(entry.minute) >= limits.rpm:
            # Back when the oldest request in the window ages out, not a
            # round minute - that is the actual moment a slot frees up.
            oldest = entry.minute[0] if entry.minute else moment
            return Verdict(False,
                           f"at {limits.rpm} requests/minute",
                           max(1.0, oldest + MINUTE - moment))
        return Verdict(True)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        self._load()
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for key, entry in self._data.items():
            entry.roll(now)
            provider, _, model = key.partition("::")
            limits = limits_for(provider, model)
            out[key] = {**entry.to_dict(),
                        "limits": limits.to_dict(),
                        "verdict": self.check(provider, model, now).to_dict()}
        return out

    def flush(self) -> None:
        self._save(force=True)


ledger = Ledger()


# ---------------------------------------------------------------------------
# Listening to the provider
# ---------------------------------------------------------------------------

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(raw: Any) -> float | None:
    """'58s', '1m30s', '2h', '750ms', or a bare number of seconds."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    total, found = 0.0, False
    for amount, unit in _DURATION.findall(text):
        try:
            total += float(amount) * _UNIT_SECONDS[unit.lower()]
            found = True
        except (ValueError, KeyError):
            continue
    return total if found else None


def _header(headers: Any, *names: str) -> str | None:
    for name in names:
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value not in (None, ""):
            return str(value)
    return None


def note_headers(provider: str, model: str, headers: Any) -> dict[str, Any]:
    """Read whatever the response says about what is left.

    Returns what it understood, mostly so the tests and the UI can see it.
    Anything it cannot parse is ignored rather than guessed at.
    """
    seen: dict[str, Any] = {}
    if headers is None:
        return seen

    for what, remaining_names, limit_names, reset_names in (
        ("requests",
         ("x-ratelimit-remaining-requests", "x-ratelimit-requests-remaining",
          "ratelimit-remaining"),
         ("x-ratelimit-limit-requests", "x-ratelimit-requests-limit",
          "ratelimit-limit"),
         ("x-ratelimit-reset-requests", "x-ratelimit-requests-reset",
          "ratelimit-reset")),
        ("tokens",
         ("x-ratelimit-remaining-tokens", "x-ratelimit-tokens-remaining"),
         ("x-ratelimit-limit-tokens", "x-ratelimit-tokens-limit"),
         ("x-ratelimit-reset-tokens", "x-ratelimit-tokens-reset")),
    ):
        raw_remaining = _header(headers, *remaining_names)
        if raw_remaining is None:
            continue
        try:
            remaining = float(raw_remaining)
        except ValueError:
            continue
        reset = parse_duration(_header(headers, *reset_names))
        seen[what] = {"remaining": remaining, "reset": reset}

        limit_raw = _header(headers, *limit_names)
        if limit_raw is not None:
            try:
                seen[what]["limit"] = float(limit_raw)
            except ValueError:
                pass

        if remaining <= 0:
            # Exact, not inferred: nothing left and the provider said when.
            ledger.hold(provider, model,
                        reset if reset else 60.0,
                        f"no {what} left on this key"
                        + (f"; back in {int(reset)}s" if reset else ""))

    _remember_inferred(provider, model, seen)
    return seen


def _remember_inferred(provider: str, model: str, seen: dict[str, Any]) -> None:
    """Keep a limit the headers implied, for display and as a last resort.

    The window has to be inferred from the reset interval: 'limit-requests'
    means per-minute at one provider and per-day at another, so the name alone
    decides nothing. Anything the user has typed in overrides this.
    """
    requests = seen.get("requests") or {}
    limit, reset = requests.get("limit"), requests.get("reset")
    if not limit or reset is None:
        return
    field_name = "rpm" if reset <= 120 else "rpd"
    store = dict(settings.get("learned_budgets") or {})
    entry = dict(store.get(key_of(provider, model)) or {})
    limits = dict(entry.get("limits") or {})
    if limits.get(field_name) == int(limit):
        return
    limits[field_name] = int(limit)
    entry["limits"] = limits
    entry["seen_at"] = time.time()
    entry["from"] = "response headers"
    store[key_of(provider, model)] = entry
    settings.set("learned_budgets", store)
