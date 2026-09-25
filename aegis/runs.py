"""In-flight runs: the agent keeps working when the window is closed.

A run is an agent loop executing as a background task, writing its events into a
buffer. HTTP connections *subscribe* to that buffer rather than driving it, so:

  * closing the window does not stop the run;
  * reopening replays everything missed and then continues live;
  * a pending approval is still there waiting, because the approval gate lives
    server-side and the loop is still blocked on it.

Persistence happens inside the run task, not in the streaming handler. If the
browser goes away mid-run the database still ends up correct.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, AsyncIterator

from . import agent, store
from .config import settings
from .providers.base import Provider
from .tools.approval import gate

MAX_BUFFER = 4000          # events retained per run; runs are minutes, not days


class PartsBuilder:
    """Builds the display part list, server-side.

    This mirrors applyEvent() in the front end exactly, so a reloaded chat looks
    the same as it did live. One shape, built in two places - so it is tested
    against a real run rather than assumed.
    """

    def __init__(self) -> None:
        self.parts: list[dict[str, Any]] = []
        self.text: list[str] = []
        self.usage: dict[str, int] | None = None
        self.error: str = ""

    def apply(self, event: dict[str, Any]) -> None:
        etype = event.get("type")

        if etype == "delta":
            self.text.append(event.get("text", ""))
            if self.parts and self.parts[-1]["kind"] == "text":
                self.parts[-1]["text"] += event.get("text", "")
            else:
                self.parts.append({"kind": "text", "text": event.get("text", "")})

        elif etype == "notice":
            self.parts.append({"kind": "notice", "text": event.get("text", "")})

        elif etype in ("route_pick", "agent_pick"):
            self.parts.append({"kind": "meta", "text": event.get("text", "")})

        elif etype == "boost_revision":
            # Fold the first draft away; the revision streams in after it.
            for part in self.parts:
                if part["kind"] == "text":
                    part["kind"] = "draft"
            self.text = []
            self.parts.append({"kind": "review", "text": event.get("text", ""),
                               "verdict": event.get("verdict", "")})

        elif etype == "step":
            self.parts.append({"kind": "step", "n": event.get("n")})

        elif etype == "tool_call":
            import json as _json
            self.parts.append({
                "kind": "tool", "id": event.get("id"), "name": event.get("name"),
                "server": event.get("server"), "risk": event.get("risk"),
                "done": False, "blobs": [],
                "argsText": _json.dumps(event.get("arguments") or {})[:300],
            })

        elif etype == "tool_result":
            part = self._find("tool", event.get("id"))
            if part:
                part.update({"done": True, "ok": event.get("ok"),
                             "result": event.get("preview"),
                             "images": event.get("images", 0)})

        elif etype == "approval_request":
            self.parts.append({
                "kind": "approval", "id": event.get("id"),
                "tool": event.get("tool"), "server": event.get("server"),
                "risk": event.get("risk"), "summary": event.get("summary"),
                "resolved": False,
            })

        elif etype == "approval_resolved":
            # The approval part is keyed by its own id, the tool call by the
            # call id; match the most recent unresolved approval instead.
            for part in reversed(self.parts):
                if part["kind"] == "approval" and not part.get("resolved"):
                    part.update({"resolved": True,
                                 "approved": event.get("approved")})
                    break

        elif etype == "usage":
            self.usage = {"input": event.get("input", 0),
                          "output": event.get("output", 0)}

        elif etype == "error":
            self.error = event.get("text", "")

    def _find(self, kind: str, ident: Any) -> dict[str, Any] | None:
        for part in reversed(self.parts):
            if part["kind"] == kind and part.get("id") == ident:
                return part
        return None

    def attach_blobs(self, call_id: str, blob_ids: list[str]) -> None:
        part = self._find("tool", call_id)
        if part is not None:
            part["blobs"] = blob_ids

    @property
    def joined_text(self) -> str:
        return "".join(self.text)


class Run:
    def __init__(self, chat_id: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.chat_id = chat_id
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.stop_reason = ""
        self.started = time.time()
        self.finished = 0.0
        self.task: asyncio.Task | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.anchor_seq: int = -1

    # -- fan-out ----------------------------------------------------------

    def emit(self, event: dict[str, Any]) -> None:
        """Buffer an event and push it to every live subscriber."""
        self.events.append(event)
        if len(self.events) > MAX_BUFFER:
            del self.events[: len(self.events) - MAX_BUFFER]
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def finish(self, stop_reason: str = "stop") -> None:
        self.done = True
        self.stop_reason = stop_reason
        self.finished = time.time()
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, from_index: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Replay what was missed, then follow live until the run ends."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        backlog = self.events[max(0, from_index):]
        was_done = self.done
        self.subscribers.add(queue)
        try:
            for event in backlog:
                yield event
            if was_done:
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            self.subscribers.discard(queue)

    def status(self) -> dict[str, Any]:
        return {"run_id": self.id, "chat_id": self.chat_id,
                "running": not self.done, "events": len(self.events),
                "stop_reason": self.stop_reason,
                "elapsed": round((self.finished or time.time()) - self.started, 1)}


class RunRegistry:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    def get(self, chat_id: str) -> Run | None:
        return self.runs.get(chat_id)

    def is_running(self, chat_id: str) -> bool:
        run = self.runs.get(chat_id)
        return bool(run and not run.done)

    def running_chat_ids(self) -> list[str]:
        return [cid for cid, run in self.runs.items() if not run.done]

    def start(self, chat_id: str, provider: Provider, model: str, *,
              use_tools: bool = True, approval_mode: str = "interactive",
              **opts: Any) -> Run:
        existing = self.runs.get(chat_id)
        if existing and not existing.done:
            return existing

        run = Run(chat_id)
        self.runs[chat_id] = run
        run.task = asyncio.create_task(
            self._drive(run, provider, model, use_tools=use_tools,
                        approval_mode=approval_mode, **opts))
        return run

    async def stop(self, chat_id: str) -> bool:
        run = self.runs.get(chat_id)
        if not run or run.done:
            return False
        # Release anything blocked on approval first, or cancelling deadlocks.
        gate.cancel_all()
        if run.task:
            run.task.cancel()
            try:
                await run.task
            except (asyncio.CancelledError, Exception):
                pass
        if not run.done:
            run.emit({"type": "notice", "text": "\n[run stopped]\n"})
            run.finish("stopped")
        return True

    async def shutdown(self) -> None:
        for chat_id in list(self.runs):
            await self.stop(chat_id)

    # -- the run body -----------------------------------------------------

    async def _drive(self, run: Run, provider: Provider, model: str, *,
                     use_tools: bool, approval_mode: str = "interactive",
                     **opts: Any) -> None:
        from . import agents as agents_mod
        from . import skills
        from . import tools as tools_mod

        chat_id = run.chat_id
        builder = PartsBuilder()

        from . import projects, superagent

        # v2: the run knows its project, so tools default to its workspace.
        project = str(opts.pop("project", "") or projects.project_of(chat_id))
        if project and not projects.get(project):
            project = ""
        projects.set_current(project, chat_id)
        pinned = list((projects.get(project) or {}).get("skills") or []) \
            if project else []

        from . import custom_agents
        agent_def = custom_agents.get(str(opts.pop("agent", "") or ""))
        if agent_def:
            pinned = [*pinned, *[s for s in agent_def.get("skills") or []
                                 if s not in pinned]]

        from . import personal
        local = is_local_provider(provider)
        personal.set_local(local)

        messages = store.transcript_for_model(chat_id)
        routing = superagent.route(messages, pinned)

        # agent__spawn is bound to this run's provider, which is what stops a
        # subagent getting one: the plain catalogue has no spawn tool in it.
        # v2.1: text-only models get tools too (text protocol), CLIs excepted -
        # they bring their own.
        if use_tools and (provider.supports_tools
                          or getattr(provider, "kind", "") != "cli"):
            spawn = agents_mod.tools(provider, model, on_event=run.emit)
            available = superagent.filter_tools(tools_mod.catalogue(spawn),
                                                routing)
            if agent_def:
                # An agent's own tool list replaces the per-message diet.
                available = custom_agents.filter_tools(
                    tools_mod.catalogue(spawn), agent_def)
        else:
            available = []

        from . import boost, taskroute
        prof = taskroute.profile(messages)
        last_text = next((str(m.get("content") or "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        boost_override = str(opts.pop("boost", "") or "") or \
            (agent_def.get("boost", "") if agent_def else "")
        boosted = boost.applies(prof.kind, routing.preload, last_text,
                                boost_override)

        system = superagent.system_prompt(
            messages, tools=available, project=project,
            local_model=local,
            routing=routing)
        if agent_def:
            system += "\n\n---\n\n" + custom_agents.system_block(agent_def)
            event = {"type": "agent_pick", "text": f"Agent: {agent_def['name']}"}
            builder.apply(event)
            run.emit(event)
        if boosted:
            system += "\n\n---\n\n" + boost.PLAN_NOTE
        messages.insert(0, {"role": "system", "content": system})
        if routing.preload:
            event = {"type": "agent_pick", "specialists": routing.preload,
                     "text": "Specialists: " + ", ".join(routing.preload)}
            builder.apply(event)
            run.emit(event)

        # The anchor row is both the first canonical assistant message of the
        # turn and the row that carries the whole turn's display parts.
        run.anchor_seq = store.add_message(chat_id, "assistant", "",
                                           meta={"display": True}, parts=[])
        first_turn_used = False
        last_turn_seq = run.anchor_seq
        last_turn_text = ""
        last_checkpoint = 0.0
        stop_reason = "stop"

        def checkpoint(force: bool = False) -> None:
            nonlocal last_checkpoint
            now = time.time()
            if not force and now - last_checkpoint < 1.0:
                return
            last_checkpoint = now
            store.update_message(
                chat_id, run.anchor_seq,
                parts=builder.parts,
                tokens_in=(builder.usage or {}).get("input", 0),
                tokens_out=(builder.usage or {}).get("output", 0))

        try:
            async for event in agent.run(provider, model, messages,
                                         tools=available,
                                         approval_mode=approval_mode,
                                         max_steps=int(opts.pop("max_steps", None)
                                                       or settings.get("max_steps", 15)),
                                         **opts):
                etype = event.get("type")

                if etype == "_turn":
                    meta: dict[str, Any] = {"display": not first_turn_used}
                    if event.get("tool_calls"):
                        meta["tool_calls"] = event["tool_calls"]
                    last_turn_text = event.get("text", "")
                    if not first_turn_used:
                        first_turn_used = True
                        last_turn_seq = run.anchor_seq
                        store.update_message(chat_id, run.anchor_seq,
                                             content=event.get("text", ""),
                                             meta={**meta, "display": True})
                    else:
                        last_turn_seq = store.add_message(
                            chat_id, "assistant", event.get("text", ""),
                            meta={**meta, "display": False})
                    continue

                if etype == "_tool_message":
                    images = event.get("images") or []
                    seq = store.add_message(
                        chat_id, "tool", event.get("content", ""),
                        meta={"tool_call_id": event.get("tool_call_id", ""),
                              "name": event.get("name", ""),
                              "is_error": bool(event.get("is_error")),
                              "display": False},
                        images=images)
                    if images:
                        stored = store.raw_messages(chat_id)
                        ids = next((m["meta"].get("image_ids", [])
                                    for m in stored if m["seq"] == seq), [])
                        builder.attach_blobs(event.get("tool_call_id", ""), ids)
                    checkpoint(force=True)
                    continue

                if etype == "done":
                    stop_reason = event.get("stop_reason", "stop")

                builder.apply(event)
                run.emit(event)
                checkpoint()

            # v2.1 Boost: a second model reviews the finished answer; if it
            # finds real problems, the answer is rewritten with that feedback.
            if boosted and stop_reason == "stop" and not builder.error and \
                    len(last_turn_text.strip()) >= boost.MIN_ANSWER_CHARS:
                verdict, reviewer = await boost.review(provider, model,
                                                       last_text, last_turn_text)
                if verdict and boost.is_clean(verdict):
                    event = {"type": "route_pick",
                             "text": f"reviewed by {reviewer} — no problems found"}
                    builder.apply(event)
                    run.emit(event)
                elif verdict:
                    event = {"type": "boost_revision", "reviewer": reviewer,
                             "text": f"reviewed by {reviewer} — improving the answer",
                             "verdict": verdict[:1500]}
                    builder.apply(event)
                    run.emit(event)
                    revised: list[str] = []
                    async for ev in boost.revise(provider, model, messages,
                                                 last_turn_text, verdict):
                        if ev.get("type") == "delta":
                            revised.append(ev.get("text", ""))
                        if ev.get("type") == "error":
                            note = {"type": "notice",
                                    "text": "\n[revision failed — keeping the draft]\n"}
                            builder.apply(note)
                            run.emit(note)
                            break
                        builder.apply(ev)
                        run.emit(ev)
                        checkpoint()
                    final = "".join(revised).strip()
                    if final:
                        store.update_message(chat_id, last_turn_seq, content=final)

        except asyncio.CancelledError:
            builder.apply({"type": "notice", "text": "\n[run stopped]\n"})
            stop_reason = "stopped"
            raise
        except Exception as exc:
            builder.apply({"type": "error",
                           "text": f"{type(exc).__name__}: {exc}"})
            run.emit({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            stop_reason = "error"
        finally:
            if builder.error:
                current = store.raw_messages(chat_id)
                anchor = next((m for m in current if m["seq"] == run.anchor_seq), None)
                meta = dict(anchor["meta"]) if anchor else {"display": True}
                meta["error"] = builder.error
                store.update_message(chat_id, run.anchor_seq, meta=meta)
            checkpoint(force=True)
            if not first_turn_used:
                store.update_message(chat_id, run.anchor_seq,
                                     content=builder.joined_text)
            store.touch(chat_id, provider.key, model)
            run.finish(stop_reason)


def is_local_provider(provider: Provider) -> bool:
    """True when every model this run could reach runs on this PC."""
    kind = getattr(provider, "kind", "")
    if kind == "local":
        return True
    route = getattr(provider, "route", None)
    if kind == "route" and route is not None and route.candidates:
        return all(c.provider in ("ollama", "lmstudio", "llamacpp")
                   for c in route.candidates)
    return False


registry = RunRegistry()
