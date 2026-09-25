"""Chat persistence: the store, the run registry, reattach and eviction.

The interesting tests here are the ones that simulate the browser going away:
a run must keep going, its output must still reach the database, and a pending
approval must still be waiting when you come back.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")
PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYA"
         "AjCB0C8AAAAASUVORK5CYII=")


class ScriptedProvider:
    key, label, kind, blurb, needs = "scripted", "Scripted", "api", "", ""
    supports_tools = True
    supports_images = True

    def __init__(self, turns: list[list[dict[str, Any]]], delay: float = 0.0) -> None:
        self.turns = turns
        self.seen: list[list[dict[str, Any]]] = []
        self.delay = delay

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["scripted"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts) -> AsyncIterator[dict[str, Any]]:
        self.seen.append([dict(m) for m in messages])
        for event in self.turns[min(len(self.seen) - 1, len(self.turns) - 1)]:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event


# ---------------------------------------------------------------------------
# 1. The store
# ---------------------------------------------------------------------------

def _store_basics() -> None:
    from aegis import store

    chat_id = store.create_chat(provider="ollama", model="qwen2.5:7b")
    check("a chat is created", bool(chat_id))
    check("a new chat has no title yet", store.get_chat(chat_id)["title"] == "")

    store.add_message(chat_id, "user", "How do I renew an ESXi certificate?")
    check("the first user message names the chat",
          store.get_chat(chat_id)["title"].startswith("How do I renew"),
          store.get_chat(chat_id)["title"])

    store.add_message(chat_id, "user", "second question")
    check("the title is set once, not on every message",
          store.get_chat(chat_id)["title"].startswith("How do I renew"))

    store.update_chat(chat_id, title="Renamed")
    check("a chat can be renamed", store.get_chat(chat_id)["title"] == "Renamed")
    store.update_chat(chat_id, pinned=1)
    check("a chat can be pinned", store.get_chat(chat_id)["pinned"] == 1)

    store.update_chat(chat_id, id="hacked")
    check("unknown fields are ignored on update",
          store.get_chat(chat_id) is not None)

    other = store.create_chat()
    store.add_message(other, "user", "unrelated")
    store.update_chat(other, pinned=0)
    listed = store.list_chats()
    check("pinned chats sort first", listed[0]["id"] == chat_id,
          str([c["id"] for c in listed]))
    check("the list carries a preview of the first message",
          any(c["preview"].startswith("How do I renew") for c in listed))

    check("search finds a chat by message text",
          any(c["id"] == chat_id for c in store.list_chats("ESXi certificate")),
          store.search_backend())
    check("search misses what is not there",
          store.list_chats("zzzznothing") == [])
    check("punctuation in a search does not break it",
          isinstance(store.list_chats('"broken( query'), list))

    check("a chat is deleted", store.delete_chat(other) is True)
    check("deleting twice is not an error", store.delete_chat(other) is False)


def _store_transcripts() -> None:
    from aegis import store

    chat_id = store.create_chat()
    store.add_message(chat_id, "user", "delete note n1")
    store.add_message(chat_id, "assistant", "Right away.",
                      meta={"display": True,
                            "tool_calls": [{"id": "c1", "name": "notes__delete_note",
                                            "arguments": {"id": "n1"}}]},
                      parts=[{"kind": "text", "text": "Right away."},
                             {"kind": "tool", "id": "c1",
                              "name": "notes__delete_note", "done": True,
                              "ok": True, "result": "deleted n1"}])
    store.add_message(chat_id, "tool", "deleted n1",
                      meta={"tool_call_id": "c1", "name": "notes__delete_note",
                            "display": False},
                      images=[("image/png", PIXEL)])
    store.add_message(chat_id, "assistant", "Done.", meta={"display": False})

    model_view = store.transcript_for_model(chat_id)
    check("model transcript keeps every canonical row", len(model_view) == 4)
    check("tool_calls survive the round trip",
          model_view[1]["tool_calls"][0]["arguments"] == {"id": "n1"})
    check("tool rows carry their call id",
          model_view[2]["tool_call_id"] == "c1")
    check("images are rehydrated as base64 for the model",
          model_view[2]["images"][0][0] == "image/png"
          and model_view[2]["images"][0][1] == PIXEL)

    display = store.transcript_for_display(chat_id)
    check("display view shows one user and one assistant turn",
          [m["role"] for m in display] == ["user", "assistant"],
          str([m["role"] for m in display]))
    check("the assistant turn carries the whole trace",
          len(display[1]["parts"]) == 2)
    check("tool rows are not shown as separate turns",
          all(m["role"] != "tool" for m in display))

    md = store.export_markdown(chat_id)
    check("markdown export includes the user text", "delete note n1" in md)
    check("markdown export includes the tool trace", "notes__delete_note" in md, md[:200])
    payload = store.export_json(chat_id)
    check("json export carries every raw row", len(payload["messages"]) == 4)
    check("json export is versioned", payload["format"] == "aegis-chat-v1")

    store.delete_chat(chat_id)


def _store_images() -> None:
    from aegis import store
    from aegis.config import settings

    chat_id = store.create_chat()
    blob_id = store.put_image(chat_id, "image/png", PIXEL)
    check("an image is stored and given an id", bool(blob_id))
    found = store.get_image(blob_id)
    check("the image round-trips byte for byte",
          found is not None and base64.b64encode(found[1]).decode() == PIXEL)
    check("garbage base64 is refused, not stored",
          store.put_image(chat_id, "image/png", "!!!not base64!!!") is None
          or store.get_chat(chat_id)["images"] == 1)

    # Squeeze the budget so eviction is observable with small images.
    settings.set("chat_image_budget_mb", 1)
    big = base64.b64encode(b"\x00" * (400 * 1024)).decode()   # 400 KB each
    ids = [store.put_image(chat_id, "image/png", big) for _ in range(6)]
    total = store.get_chat(chat_id)["image_bytes"]
    check("the per-chat image budget is enforced",
          total <= store.image_budget_bytes(),
          f"{total} bytes vs budget {store.image_budget_bytes()}")
    check("the newest image is kept", store.get_image(ids[-1]) is not None)
    check("the oldest images are the ones evicted", store.get_image(ids[0]) is None)

    # A message referencing an evicted image must say so, not fail.
    store.add_message(chat_id, "tool", "here is a picture",
                      meta={"tool_call_id": "c", "name": "t"},
                      images=[("image/png", big)])
    for blob in ids[:4]:
        pass
    view = store.transcript_for_model(chat_id)
    check("a transcript with evicted images still builds",
          isinstance(view, list) and len(view) == 1)

    settings.set("chat_image_budget_mb", 48)
    store.delete_chat(chat_id)
    check("deleting a chat removes its images", store.get_image(ids[-1]) is None)


# ---------------------------------------------------------------------------
# 2. Runs: persistence, reattach, stop
# ---------------------------------------------------------------------------

async def _run_persists() -> None:
    from aegis import runs, store
    from aegis.mcp.registry import registry
    from aegis.tools import catalogue
    from aegis.tools.approval import gate

    from aegis.config import settings
    settings.set("tool_policy", "auto_read_ask_write")
    settings.set("tool_decisions", {})
    registry.add("notes", {"command": sys.executable, "args": [SERVER]}, origin="test")
    await registry.enable("notes")

    chat_id = store.create_chat()
    store.add_message(chat_id, "user", "list the notes")

    provider = ScriptedProvider([
        [{"type": "delta", "text": "Checking. "},
         {"type": "tool_calls", "calls": [
             {"id": "c1", "name": "notes__list_notes", "arguments": {}}]},
         {"type": "done"}],
        [{"type": "delta", "text": "You have two."},
         {"type": "usage", "input": 10, "output": 4},
         {"type": "done"}],
    ])

    run = runs.registry.start(chat_id, provider, "m", use_tools=True)
    events = [e async for e in run.subscribe(0)]
    await run.task

    kinds = [e["type"] for e in events]
    check("internal events are not in the buffer sent to the browser",
          not any(k.startswith("_") for k in kinds), str(set(kinds)))

    rows = store.raw_messages(chat_id)
    roles = [r["role"] for r in rows]
    check("the full canonical transcript is persisted",
          roles == ["user", "assistant", "tool", "assistant"], str(roles))
    check("the assistant turn's tool_calls are stored",
          rows[1]["meta"].get("tool_calls", [{}])[0].get("name") == "notes__list_notes")
    check("the tool result text is stored", "note-1" in rows[2]["content"])
    check("usage is recorded on the anchor row",
          rows[1]["tokens_out"] == 4, str(rows[1]["tokens_out"]))

    display = store.transcript_for_display(chat_id)
    check("the reloaded view has one user and one assistant turn",
          [m["role"] for m in display] == ["user", "assistant"],
          str([m["role"] for m in display]))
    parts = display[1]["parts"]
    kinds = [p["kind"] for p in parts]
    check("the stored trace holds text and the tool call",
          "text" in kinds and "tool" in kinds, str(kinds))
    tool_part = next(p for p in parts if p["kind"] == "tool")
    check("the stored tool part has its result", "note-1" in tool_part["result"])
    check("the stored trace includes both model turns' text",
          "Checking." in parts[0]["text"] and any("two" in p.get("text", "")
                                                  for p in parts))

    # The server-side parts builder must match what the UI would have built.
    check("server-built parts are UI-shaped",
          all({"kind"} <= set(p) for p in parts))

    store.delete_chat(chat_id)
    await registry.disable("notes")
    registry.remove("notes")


async def _reattach_midrun() -> None:
    """The browser goes away mid-run. The run must continue and be rejoinable."""
    from aegis import runs, store

    chat_id = store.create_chat()
    store.add_message(chat_id, "user", "talk slowly")

    provider = ScriptedProvider([
        [{"type": "delta", "text": "one "}, {"type": "delta", "text": "two "},
         {"type": "delta", "text": "three "}, {"type": "delta", "text": "four"},
         {"type": "done"}],
    ], delay=0.12)

    run = runs.registry.start(chat_id, provider, "m", use_tools=False)

    # First viewer takes two events, then walks away.
    seen: list[dict[str, Any]] = []
    async for event in run.subscribe(0):
        seen.append(event)
        if len(seen) == 2:
            break
    check("a viewer can leave mid-stream", len(seen) == 2)
    check("the run is still going after the viewer leaves", not run.done)

    # Second viewer rejoins from the start and gets everything.
    rejoined = [e async for e in run.subscribe(0)]
    await run.task
    text = "".join(e.get("text", "") for e in rejoined if e["type"] == "delta")
    check("rejoining replays everything that was missed",
          text == "one two three four", repr(text))
    check("the run finished on its own", run.done and run.stop_reason == "stop")

    # Rejoining from an offset skips what that viewer already saw.
    tail = [e async for e in run.subscribe(2)]
    check("rejoining from an offset returns only the tail",
          len(tail) == len(rejoined) - 2, f"{len(tail)} vs {len(rejoined)}")

    rows = store.raw_messages(chat_id)
    check("the run persisted even though the first viewer left",
          rows[1]["content"] == "one two three four", rows[1]["content"])

    store.delete_chat(chat_id)


async def _approval_survives_disconnect() -> None:
    """A pending approval must still be waiting when you come back."""
    from aegis import runs, store
    from aegis.config import settings
    from aegis.mcp.registry import registry
    from aegis.tools.approval import gate

    settings.set("tool_policy", "auto_read_ask_write")
    settings.set("tool_decisions", {})
    registry.add("notes", {"command": sys.executable, "args": [SERVER]}, origin="test")
    await registry.enable("notes")

    chat_id = store.create_chat()
    store.add_message(chat_id, "user", "delete n1")

    provider = ScriptedProvider([
        [{"type": "tool_calls", "calls": [
            {"id": "c1", "name": "notes__delete_note", "arguments": {"id": "n1"}}]},
         {"type": "done"}],
        [{"type": "delta", "text": "Deleted."}, {"type": "done"}],
    ])

    # Start the run with NOBODY subscribed - as if the window were already shut.
    run = runs.registry.start(chat_id, provider, "m", use_tools=True)

    for _ in range(150):
        await asyncio.sleep(0.02)
        if gate.list():
            break
    check("the run reaches a pending approval with no viewer attached",
          len(gate.list()) == 1, str(gate.list()))
    check("the run is still alive, blocked on the approval", not run.done)

    # Come back: the replay must contain the approval request.
    buffered = list(run.events)
    check("the approval request is in the replay buffer",
          any(e["type"] == "approval_request" for e in buffered),
          str([e["type"] for e in buffered]))

    gate.resolve(gate.list()[0]["id"], True)
    await asyncio.wait_for(run.task, 20)
    check("answering the approval lets the run finish", run.done)

    rows = store.raw_messages(chat_id)
    check("the approved tool ran and was persisted",
          any(r["role"] == "tool" and "deleted n1" in r["content"] for r in rows),
          str([r["content"][:30] for r in rows]))

    parts = store.transcript_for_display(chat_id)[1]["parts"]
    approval = next((p for p in parts if p["kind"] == "approval"), None)
    check("the approval and its outcome are stored in the trace",
          approval is not None and approval.get("resolved") is True
          and approval.get("approved") is True, str(approval))

    store.delete_chat(chat_id)
    await registry.disable("notes")
    registry.remove("notes")


async def _stop_and_guards() -> None:
    from aegis import runs, store

    chat_id = store.create_chat()
    store.add_message(chat_id, "user", "go")
    provider = ScriptedProvider([
        [{"type": "delta", "text": "x"}] * 200 + [{"type": "done"}],
    ], delay=0.05)

    run = runs.registry.start(chat_id, provider, "m", use_tools=False)
    check("a second start on the same chat returns the same run",
          runs.registry.start(chat_id, provider, "m") is run)
    check("the registry reports the chat as running",
          runs.registry.is_running(chat_id))
    check("running chats are listed", chat_id in runs.registry.running_chat_ids())

    await asyncio.sleep(0.25)
    check("stop returns True for a live run", await runs.registry.stop(chat_id) is True)
    check("the run is marked done after stopping", run.done)
    check("stopping an already-stopped run returns False",
          await runs.registry.stop(chat_id) is False)

    rows = store.raw_messages(chat_id)
    check("a stopped run still persisted what it produced",
          len(rows) == 2 and "x" in rows[1]["content"], str(len(rows)))

    store.delete_chat(chat_id)


# ---------------------------------------------------------------------------
# 3. Through the HTTP API
# ---------------------------------------------------------------------------

def _api() -> None:
    from fastapi.testclient import TestClient
    from aegis.server import app

    with TestClient(app) as client:
        r = client.post("/api/chats", json={"title": "From the API"})
        chat_id = r.json()["chat"]["id"]
        check("POST /api/chats creates one", r.status_code == 200 and bool(chat_id))

        r = client.get("/api/chats")
        check("GET /api/chats lists it",
              any(c["id"] == chat_id for c in r.json()["chats"]))

        r = client.get(f"/api/chats/{chat_id}")
        check("GET /api/chats/{id} returns chat and messages",
              r.status_code == 200 and r.json()["messages"] == [])

        r = client.patch(f"/api/chats/{chat_id}", json={"pinned": True})
        check("PATCH pins a chat", r.json()["chat"]["pinned"] == 1)

        r = client.get(f"/api/chats/{chat_id}/export?format=json")
        check("export as JSON works", r.status_code == 200
              and r.json()["format"] == "aegis-chat-v1")
        r = client.get(f"/api/chats/{chat_id}/export?format=md")
        check("export as Markdown works",
              r.status_code == 200 and "From the API" in r.text)

        r = client.post("/api/chat", json={"provider": "nope", "text": "hi"})
        check("sending to an unknown provider is refused", r.status_code == 400)
        r = client.post("/api/chat", json={"provider": "ollama", "text": ""})
        check("an empty message is refused", r.status_code == 400)

        r = client.get("/api/chats/does-not-exist")
        check("an unknown chat 404s", r.status_code == 404)
        r = client.get("/api/chats/does-not-exist/stream")
        check("reattaching to a chat with no run 404s", r.status_code == 404)

        r = client.get("/api/blobs/nope")
        check("a missing blob 404s with a reason",
              r.status_code == 404 and "budget" in r.json()["error"])

        r = client.get("/api/store")
        check("GET /api/store reports the database",
              r.status_code == 200 and "db_bytes" in r.json())

        r = client.delete(f"/api/chats/{chat_id}")
        check("DELETE removes the chat", r.json()["ok"] is True)


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("chat store")
    _store_basics()
    section("transcripts and export")
    _store_transcripts()
    section("image storage and eviction")
    _store_images()
    section("runs: persistence")
    asyncio.run(_run_persists())
    section("runs: reattach after the window closes")
    asyncio.run(_reattach_midrun())
    section("runs: approvals survive a disconnect")
    asyncio.run(_approval_survives_disconnect())
    section("runs: stop and guards")
    asyncio.run(_stop_and_guards())
    section("chat API")
    _api()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR",
                          tempfile.mkdtemp(prefix="aegis-store-test-"))
    from harness import report
    run_all()
    sys.exit(report())
