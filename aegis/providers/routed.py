"""A route, wearing a Provider's clothes.

RoutedProvider implements the ordinary Provider contract, so the agent loop,
the run registry and the UI need to know nothing about routing. Inside, it
walks the route's candidates and handles failure.

Two failure paths, because they are genuinely different:

  nothing streamed yet    Switch silently. The user never sees that the first
                          model refused - they just get their answer.

  text already streamed   Keep what was written, tell the user in the trace
                          which model took over, and ask the next one to
                          continue from there. An honest seam beats a lost
                          reply, and beats paying twice for a restart.

A tool call that was mid-flight when the model died is not continued - the
partial arguments are worthless, so that turn restarts on the next model.
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from .. import budget, router
from ..router import Candidate, Route, book
from .base import Event, Message, Provider, not_ok, ok

CONTINUE_NUDGE = (
    "The previous model stopped mid-sentence because of a service failure, not "
    "because it had finished. Carry straight on from exactly where that text "
    "ends. Do not repeat any of it, do not greet, do not summarise what came "
    "before - just continue the sentence.")


class RoutedProvider(Provider):
    kind = "route"
    needs = ""
    supports_tools = True
    supports_images = True

    def __init__(self, route: Route) -> None:
        self.route = route
        self.key = f"route:{route.key}"
        self.label = route.label
        self.blurb = route.description or f"{len(route.candidates)} models in order"
        self.last_used: Candidate | None = None

    # -- contract ---------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        result = router.plan(self.route)
        if result.ok:
            first = result.usable[0]
            detail = f"{len(result.usable)} model(s) ready, next: {first.model}"
            if result.resting:
                detail += f" ({len(result.resting)} resting)"
            return ok(detail)
        if not self.route.candidates:
            return not_ok("No models in this route",
                          "Add some on the Routes tab.")
        return not_ok("Every model in this route is unavailable",
                      result.explain())

    async def models(self) -> list[str]:
        return [f"{c.provider}/{c.model}" for c in self.route.candidates]

    # -- the interesting part ---------------------------------------------

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        """Walk this route (and any overflow route) until something answers.

        v2 adds, on top of the v1 failover:
          * smart ordering - the request's needs pick who goes first;
          * capability failover - "this model cannot take tools/images/this
            much text" moves on (and is remembered) instead of stopping;
          * auth failover - a rejected key rests that provider, not the route;
          * empty answers count as failures;
          * no tool-capable model left -> answer without tools, not an error;
          * an optional overflow route when this one is spent.
        """
        from .. import taskroute

        prof = taskroute.profile(messages)
        needs = router.Needs(tools=bool(opts.get("tools")), images=prof.images,
                             tokens=prof.tokens)

        chain = [self.route]
        seen = {self.route.key}
        nxt = self.route.then_route
        while nxt and nxt not in seen:
            extra = router.get_route(nxt)
            if not extra:
                break
            chain.append(extra)
            seen.add(nxt)
            nxt = extra.then_route

        last_error = ""
        state = {"carried": "", "attempted": 0, "announced": False}
        for index, route in enumerate(chain):
            if index:
                yield {"type": "notice",
                       "text": f"\n[{chain[index - 1].label} is spent — carrying "
                               f"on with {route.label}"
                               + ("" if route.allow_paid is False else
                                  " (this route may cost money)") + "]\n"}
            outcome: dict[str, Any] = {}
            async for event in self._walk(route, messages, prof, needs, opts,
                                          state, outcome):
                yield event
            if outcome.get("finished"):
                return
            last_error = outcome.get("last_error") or last_error
            if outcome.get("stop"):
                return

        if not state["attempted"]:
            first = router.plan(self.route, needs=needs)
            yield {"type": "error", "text": self._nothing_available(first)}
            return
        yield {"type": "error",
               "text": (f"Every model in '{self.route.label}' failed. "
                        f"Last: {last_error}. " + self._exhausted_advice())}

    async def _walk(self, route: Route, messages: list[Message], prof: Any,
                    needs: router.Needs, opts: dict[str, Any],
                    state: dict[str, Any],
                    outcome: dict[str, Any]) -> AsyncIterator[Event]:
        from . import registry
        from .. import taskroute

        opts = dict(opts)
        attempt = router.plan(route, needs=needs)
        if not attempt.ok and needs.tools:
            # Nothing here can use tools right now. An answer without tools
            # beats an error - say so, and carry on as plain chat.
            plain = router.Needs(tools=False, images=needs.images,
                                 tokens=needs.tokens)
            fallback = router.plan(route, needs=plain)
            if fallback.ok:
                attempt = fallback
                from ..config import settings as _settings
                if _settings.get("text_tools", True):
                    opts["_emulate_tools"] = True
                    yield {"type": "notice",
                           "text": "\n[no tool-calling model is free right now — "
                                   "continuing without tools via the text tool "
                                   "protocol]\n"}
                else:
                    opts["tools"] = None
                    yield {"type": "notice",
                           "text": "\n[no tool-capable model is free right now — "
                                   "answering without tools]\n"}
        if not attempt.ok and needs.images:
            relaxed = router.Needs(tools=bool(opts.get("tools")), images=False,
                                   tokens=needs.tokens)
            fallback = router.plan(route, needs=relaxed)
            if fallback.ok:
                attempt = fallback
        if not attempt.ok:
            outcome["last_error"] = attempt.explain()
            return

        order = (taskroute.order(attempt.usable, prof) if route.smart
                 else list(attempt.usable))
        available = registry()
        transcript = list(messages)
        providers_left = {c.provider for c in order}

        for position, candidate in enumerate(order):
            provider = available.get(candidate.provider)
            if provider is None:
                continue
            if router.book.peek(candidate.provider, "*").resting:
                continue            # blocked earlier in this same turn
            verdict = budget.ledger.check(candidate.provider, candidate.model)
            if not verdict.ok:
                outcome["last_error"] = (f"{candidate.provider}/"
                                         f"{candidate.model}: {verdict.why}")
                continue

            if state["attempted"]:
                yield {"type": "notice",
                       "text": f"\n[switched to {candidate.provider}/"
                               f"{candidate.model}]\n"}
            elif route.smart and not state["announced"] and \
                    messages and messages[-1].get("role") == "user":
                # Once per user message - not again on every tool step.
                state["announced"] = True
                yield {"type": "route_pick",
                       "text": taskroute.explain(prof, candidate),
                       "provider": candidate.provider,
                       "model": candidate.model, "profile": prof.kind}
            state["attempted"] += 1

            if state["carried"]:
                transcript = [*messages,
                              {"role": "assistant", "content": state["carried"]},
                              {"role": "user", "content": CONTINUE_NUDGE}]

            self.last_used = candidate
            started = time.time()
            produced = ""
            tool_calls_seen = False
            output_tokens = 0
            failure: tuple[str, str] | None = None
            retry_hint: float | None = None
            held_done: Event | None = None
            budget.ledger.record_request(candidate.provider, candidate.model)

            call_opts = dict(opts)
            emulate = call_opts.pop("_emulate_tools", False)
            speaker = provider
            if call_opts.get("tools") and (emulate or not provider.supports_tools
                                           or router.caps(candidate.provider,
                                                          candidate.model)["tools"] is False):
                from ..config import settings as _settings
                if _settings.get("text_tools", True):
                    from .texttools import TextToolsProvider
                    speaker = TextToolsProvider(provider)
                else:
                    call_opts["tools"] = None

            async for event in speaker.chat(transcript, candidate.model,
                                            **call_opts):
                etype = event.get("type")

                if etype == "error":
                    kind = router.classify(event.get("status"),
                                           event.get("text", ""))
                    others = providers_left - {candidate.provider}
                    if kind == router.AUTH and others:
                        # A rejected key is that provider's problem, not the
                        # route's. Rest the provider, tell the user, move on.
                        router.block_provider(candidate.provider, 15 * 60,
                                              event.get("text", ""))
                        yield {"type": "notice",
                               "text": f"\n[{candidate.provider} rejected its "
                                       f"API key — skipping it for 15 minutes. "
                                       f"Check it on the Providers tab.]\n"}
                        failure = (kind, event.get("text", ""))
                        break
                    if router.should_failover(kind):
                        failure = (kind, event.get("text", ""))
                        retry_hint = event.get("retry_after")
                        break
                    book.record_failure(candidate.provider, candidate.model,
                                        kind, event.get("text", ""),
                                        retry_after=event.get("retry_after"))
                    outcome["stop"] = True
                    yield {**event, "model": candidate.model,
                           "provider": candidate.provider, "kind": kind}
                    return

                if etype == "done":
                    held_done = event
                    continue
                if etype == "delta":
                    produced += event.get("text", "")
                elif etype == "tool_calls":
                    tool_calls_seen = True
                elif etype == "usage":
                    output_tokens = event.get("output", 0) or output_tokens
                    budget.ledger.record_tokens(
                        candidate.provider, candidate.model,
                        int(event.get("input", 0) or 0)
                        + int(event.get("output", 0) or 0))

                yield event

            if failure is None and not produced.strip() and not tool_calls_seen \
                    and position < len(order) - 1:
                failure = (router.EMPTY, "the model returned an empty answer")

            if failure is None:
                book.record_success(candidate.provider, candidate.model,
                                    output_tokens=output_tokens,
                                    seconds=time.time() - started)
                yield held_done or {"type": "done", "stop_reason": "stop"}
                outcome["finished"] = True
                return

            kind, detail = failure
            if kind == router.CAPABILITY:
                gap = router.learn_from_refusal(candidate.provider,
                                                candidate.model, detail,
                                                needs.tokens)
                yield {"type": "notice",
                       "text": f"\n[{candidate.model} can't handle "
                               f"{ {'tools': 'tools', 'vision': 'images', 'context': 'this much text'}.get(gap, 'this request') } "
                               f"— trying the next model]\n"}
            elif kind != router.AUTH:
                book.record_failure(candidate.provider, candidate.model,
                                    kind, detail, retry_after=retry_hint)
            outcome["last_error"] = f"{candidate.provider}/{candidate.model}: {kind}"

            if produced and not tool_calls_seen:
                state["carried"] += produced
                yield {"type": "notice",
                       "text": f"\n[{candidate.model} stopped: {kind}. "
                               f"Continuing on the next model.]\n"}
            elif tool_calls_seen:
                state["carried"] = ""

    # -- messages ---------------------------------------------------------

    def _nothing_available(self, attempt: router.Plan) -> str:
        if not self.route.candidates:
            return (f"The route '{self.route.label}' has no models in it. "
                    f"Add some on the Routes tab.")
        if attempt.over_budget and not attempt.resting and not attempt.excluded:
            # Nothing is broken - the allowances are simply spent. Worth saying
            # differently, because the fix is 'wait' rather than 'investigate'.
            return (f"Every model in '{self.route.label}' is out of allowance "
                    f"for now. {attempt.explain()}. {self._exhausted_advice()}")
        return (f"No model in '{self.route.label}' can run right now. "
                f"{attempt.explain()}. {self._exhausted_advice()}")

    def _exhausted_advice(self) -> str:
        if not self.route.allow_paid:
            return ("This is a free-only route, so it will not fall back to a "
                    "paid model. Switch to a paid route yourself, or wait for "
                    "the free quotas to reset.")
        return "Check the Routes tab for what each model is resting on."


def route_providers() -> list[RoutedProvider]:
    return [RoutedProvider(route) for route in router.routes().values()]
