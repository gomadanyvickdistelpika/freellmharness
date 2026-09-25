"""MCP, tools, approvals and the agent loop.

The MCP tests drive a real server process over real stdio framing - see
fake_mcp_server.py. If the handshake here passes, the protocol implementation
works; it is not a mock agreeing with itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


# ---------------------------------------------------------------------------
# 1. Real MCP round trip
# ---------------------------------------------------------------------------

async def _mcp_roundtrip() -> None:
    from aegis.mcp.protocol import MCPClient, StdioTransport, build_client

    client = build_client("notes", {"command": sys.executable, "args": [SERVER]})
    connected = await client.connect()
    check("MCP handshake completes against a real server", connected,
          client.error or "")
    if not connected:
        return

    check("serverInfo read from initialize",
          client.server_info.get("name") == "fake-notes",
          str(client.server_info))
    check("stdout banner before the first message is tolerated", connected)
    check("tools/list returns the tool set", len(client.tools) == 4,
          str(len(client.tools)))

    names = {t.name for t in client.tools}
    check("tool names parsed", {"list_notes", "delete_note", "frobnicate",
                                "make_picture"} == names, str(sorted(names)))

    schema = next(t for t in client.tools if t.name == "delete_note").input_schema
    check("input schema preserved", schema.get("required") == ["id"], str(schema))

    result = await client.call("list_notes", {})
    check("tools/call returns content", result.ok and "note-1" in result.text,
          result.text[:60])

    result = await client.call("delete_note", {"id": "abc"})
    check("tool arguments reach the server", "deleted abc" in result.text,
          result.text[:60])

    result = await client.call("make_picture", {})
    check("image content blocks are extracted",
          len(result.images) == 1 and result.images[0][0] == "image/png",
          str([m for m, _ in result.images]))
    check("text and image arrive together", "pixel" in result.text)

    result = await client.call("frobnicate", {})
    check("isError is respected", result.ok is False, result.text[:60])

    result = await client.call("no_such_tool", {})
    check("protocol error becomes a failed result, not a crash",
          result.ok is False and "failed" in result.text.lower(), result.text[:80])

    # Concurrent calls must not cross replies - the lock is the thing under test.
    results = await asyncio.gather(*(client.call("list_notes", {}) for _ in range(5)))
    check("five concurrent calls all return correctly",
          all(r.ok and "note-1" in r.text for r in results))

    await client.close()
    check("client closes cleanly", not client.connected)

    # A command that does not exist must fail loudly with a useful message.
    bad = build_client("bad", {"command": "definitely-not-a-real-binary-xyz"})
    check("missing binary fails with a clear error",
          await bad.connect() is False and "not found" in bad.error.lower(),
          bad.error)


# ---------------------------------------------------------------------------
# 2. Registry and the tool catalogue
# ---------------------------------------------------------------------------

async def _registry() -> None:
    from aegis.mcp.registry import registry
    from aegis.tools import catalogue
    from aegis.tools.base import READ, WRITE

    registry.add("notes", {"command": sys.executable, "args": [SERVER]},
                 origin="test")
    check("server stored in settings", "notes" in registry.configured())

    result = await registry.enable("notes")
    check("registry connects the server", result.get("ok") is True, str(result))
    check("registry reports the tool count", result.get("tools") == 4, str(result))

    tools = catalogue()
    by_name = {t.name: t for t in tools}
    check("tools are namespaced by server",
          "notes__list_notes" in by_name, str(sorted(by_name)[:6]))

    check("readOnlyHint honoured -> read",
          by_name["notes__list_notes"].risk == READ)
    check("destructiveHint honoured -> write",
          by_name["notes__delete_note"].risk == WRITE)
    check("unknown verb with no annotation fails closed to write",
          by_name["notes__frobnicate"].risk == WRITE)

    schema = by_name["notes__delete_note"].openai_schema()
    check("OpenAI schema shape is valid",
          schema["type"] == "function"
          and schema["function"]["name"] == "notes__delete_note"
          and schema["function"]["parameters"]["required"] == ["id"])
    aschema = by_name["notes__delete_note"].anthropic_schema()
    check("Anthropic schema shape is valid",
          aschema["name"] == "notes__delete_note" and "input_schema" in aschema)

    call = await registry.call("notes__list_notes", {})
    check("registry routes a namespaced call to the right server",
          call.ok and "note-1" in call.text)

    call = await registry.call("nope__nothing", {})
    check("unknown namespaced tool is refused", call.ok is False)

    status = registry.status()
    entry = next(s for s in status if s["name"] == "notes")
    check("status reports connected", entry["connected"] is True)
    check("status lists tools with risk", len(entry["tools"]) == 4)

    await registry.disable("notes")
    check("disable disconnects", not registry.status()[0]["connected"])
    check("disabled server exposes no tools",
          not any(t.server == "notes" for t in catalogue()))
    registry.remove("notes")


# ---------------------------------------------------------------------------
# 3. Classification heuristics
# ---------------------------------------------------------------------------

def _classification() -> None:
    from aegis.tools.base import READ, WRITE, classify, namespace

    reads = ["get_user", "listMessages", "search_files", "read_file",
             "fetch-page", "describe_table", "whoami", "screenshot",
             "gmail.search_threads"]
    for name in reads:
        check(f"{name} -> read", classify(name) == READ, classify(name))

    writes = ["send_message", "delete_file", "create_event", "update_row",
              "trash_thread", "executeQuery", "push_changes", "reply",
              "computer__click"]
    for name in writes:
        check(f"{name} -> write", classify(name) == WRITE, classify(name))

    check("nonsense name fails closed to write", classify("xyzzy_plugh") == WRITE)
    check("annotation beats the name (readOnlyHint on a write-ish name)",
          classify("delete_thing", {"readOnlyHint": True}) == READ)
    check("annotation beats the name (destructiveHint on a read-ish name)",
          classify("list_things", {"destructiveHint": True}) == WRITE)

    check("namespacing strips illegal characters",
          namespace("my server!", "do/thing") == "my_server__do_thing")
    check("namespaced names stay within the 64-char API limit",
          len(namespace("a" * 40, "b" * 60)) <= 64)


# ---------------------------------------------------------------------------
# 4. Approval gate
# ---------------------------------------------------------------------------

async def _approvals() -> None:
    from aegis.config import settings
    from aegis.tools.approval import ApprovalGate
    from aegis.tools.base import READ, WRITE

    gate = ApprovalGate()
    settings.set("tool_decisions", {})

    settings.set("tool_policy", "auto_read_ask_write")
    check("default policy auto-runs reads",
          gate.decide_without_asking("x__get", "x", READ) is True)
    check("default policy asks about writes",
          gate.decide_without_asking("x__send", "x", WRITE) is None)

    settings.set("tool_policy", "ask_always")
    check("ask_always asks even about reads",
          gate.decide_without_asking("x__get", "x", READ) is None)

    settings.set("tool_policy", "auto_all")
    check("auto_all runs writes without asking",
          gate.decide_without_asking("x__send", "x", WRITE) is True)

    settings.set("tool_policy", "per_server")
    settings.set("server_policies", {"safe": "auto_all"})
    check("per_server uses the named server's policy",
          gate.decide_without_asking("safe__send", "safe", WRITE) is True)
    check("per_server falls back for unlisted servers",
          gate.decide_without_asking("other__send", "other", WRITE) is None)

    settings.set("tool_policy", "auto_read_ask_write")
    gate.remember("x__send", "allow")
    check("a remembered allow skips the prompt",
          gate.decide_without_asking("x__send", "x", WRITE) is True)
    gate.remember("x__get", "deny")
    check("a remembered deny blocks even a read",
          gate.decide_without_asking("x__get", "x", READ) is False)
    check("remembered decisions persist in settings",
          (settings.get("tool_decisions") or {}).get("x__send") == "allow")
    gate.forget_all()
    check("forget_all clears them", not settings.get("tool_decisions"))

    # Live approve/deny through the async path.
    pending = await gate.request("x__send", "x", WRITE, {"to": "a@b.c"})
    check("a pending approval is listed", len(gate.list()) == 1)
    waiter = asyncio.create_task(gate.wait(pending))
    await asyncio.sleep(0)
    check("resolve returns True for a live approval",
          gate.resolve(pending.id, True) is True)
    approved, _ = await waiter
    check("approval resolves to allow", approved is True)
    check("resolved approvals leave the pending list", not gate.list())

    pending = await gate.request("x__send", "x", WRITE, {})
    waiter = asyncio.create_task(gate.wait(pending))
    await asyncio.sleep(0)
    gate.resolve(pending.id, False)
    approved, reason = await waiter
    check("denial resolves to deny with a reason",
          approved is False and "declined" in reason.lower(), reason)

    check("resolving an unknown id is refused", gate.resolve("nope", True) is False)


# ---------------------------------------------------------------------------
# 5. The agent loop, against a scripted provider
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """Replays a fixed list of turns, recording what it was sent."""
    key, label, kind, blurb, needs = "scripted", "Scripted", "api", "", ""
    supports_tools = True
    supports_images = True

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = turns
        self.seen: list[list[dict[str, Any]]] = []
        self.tools_offered: list[Any] = []

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["scripted"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts) -> AsyncIterator[dict[str, Any]]:
        self.seen.append([dict(m) for m in messages])
        self.tools_offered = list(opts.get("tools") or [])
        events = self.turns[min(len(self.seen) - 1, len(self.turns) - 1)]
        for event in events:
            yield event


async def _agent_loop() -> None:
    from aegis import agent
    from aegis.config import settings
    from aegis.mcp.registry import registry
    from aegis.tools import catalogue
    from aegis.tools.approval import gate

    settings.set("tool_policy", "auto_read_ask_write")
    settings.set("tool_decisions", {})
    registry.add("notes", {"command": sys.executable, "args": [SERVER]},
                 origin="test")
    await registry.enable("notes")
    tools = catalogue()

    # -- a read tool runs without asking, and the loop continues -----------
    provider = ScriptedProvider([
        [{"type": "delta", "text": "Looking. "},
         {"type": "tool_calls", "calls": [
             {"id": "c1", "name": "notes__list_notes", "arguments": {}}]},
         {"type": "done"}],
        [{"type": "delta", "text": "You have two notes."}, {"type": "done"}],
    ])
    events = [e async for e in agent.run(provider, "m",
                                         [{"role": "user", "content": "hi"}],
                                         tools=tools)]
    kinds = [e["type"] for e in events]
    check("read tool runs with no approval event", "approval_request" not in kinds)
    check("tool_call is announced before it runs", "tool_call" in kinds)
    check("tool_result is emitted", "tool_result" in kinds)
    result = next(e for e in events if e["type"] == "tool_result")
    check("tool result carries the real output", "note-1" in result["preview"])
    check("loop continues to a second model turn", len(provider.seen) == 2)
    check("the second turn sees the tool result",
          any(m.get("role") == "tool" for m in provider.seen[1]))
    check("the assistant tool_call turn is in the transcript",
          any(m.get("tool_calls") for m in provider.seen[1]))
    check("loop ends with done", kinds[-1] == "done")
    check("tools were offered to the provider", len(provider.tools_offered) > 0)

    # -- a write tool pauses for approval, and a denial is survivable ------
    provider = ScriptedProvider([
        [{"type": "tool_calls", "calls": [
            {"id": "c1", "name": "notes__delete_note",
             "arguments": {"id": "n1"}}]}, {"type": "done"}],
        [{"type": "delta", "text": "Understood, I did not delete it."},
         {"type": "done"}],
    ])

    async def deny_soon():
        for _ in range(80):
            await asyncio.sleep(0.02)
            if gate.list():
                gate.resolve(gate.list()[0]["id"], False)
                return

    denier = asyncio.create_task(deny_soon())
    events = [e async for e in agent.run(provider, "m",
                                         [{"role": "user", "content": "delete n1"}],
                                         tools=tools)]
    await denier
    kinds = [e["type"] for e in events]
    check("write tool raises an approval request", "approval_request" in kinds)
    approval = next(e for e in events if e["type"] == "approval_request")
    check("approval names the tool and the arguments",
          approval["tool"] == "notes__delete_note" and "n1" in approval["summary"],
          approval["summary"])
    denied = next(e for e in events if e["type"] == "tool_result")
    check("a denied call reports as not ok", denied["ok"] is False)
    check("the model is told it was declined, and carries on",
          len(provider.seen) == 2
          and any("declined" in str(m.get("content", "")).lower()
                  for m in provider.seen[1] if m.get("role") == "tool"))

    # -- images come back attached to the tool result ----------------------
    # make_picture has no annotation and an unrecognised verb, so it classifies
    # as a write and would block on approval. Remembering an allow is both the
    # fix and a test of the remembered-decision path inside a live run.
    gate.remember("notes__make_picture", "allow")
    check("a remembered allow lets a write-classified tool run unattended",
          gate.decide_without_asking("notes__make_picture", "notes", "write") is True)

    provider = ScriptedProvider([
        [{"type": "tool_calls", "calls": [
            {"id": "c1", "name": "notes__make_picture", "arguments": {}}]},
         {"type": "done"}],
        [{"type": "delta", "text": "I can see it."}, {"type": "done"}],
    ])
    events = [e async for e in agent.run(provider, "m",
                                         [{"role": "user", "content": "show me"}],
                                         tools=tools)]
    result = next(e for e in events if e["type"] == "tool_result")
    check("image count is reported on the tool result", result.get("images") == 1)
    tool_msg = next(m for m in provider.seen[1] if m.get("role") == "tool")
    check("the image is carried in the transcript", len(tool_msg.get("images", [])) == 1)

    # -- an unknown tool is a recoverable result, not a crash --------------
    provider = ScriptedProvider([
        [{"type": "tool_calls", "calls": [
            {"id": "c1", "name": "ghost__tool", "arguments": {}}]}, {"type": "done"}],
        [{"type": "delta", "text": "That tool does not exist."}, {"type": "done"}],
    ])
    events = [e async for e in agent.run(provider, "m",
                                         [{"role": "user", "content": "x"}],
                                         tools=tools)]
    result = next(e for e in events if e["type"] == "tool_result")
    check("an unknown tool returns a failed result", result["ok"] is False)
    check("the loop recovers from an unknown tool", len(provider.seen) == 2)

    # -- the step cap actually stops it ------------------------------------
    looper = ScriptedProvider([
        [{"type": "tool_calls", "calls": [
            {"id": "c", "name": "notes__list_notes", "arguments": {}}]},
         {"type": "done"}],
    ])
    events = [e async for e in agent.run(looper, "m",
                                         [{"role": "user", "content": "loop"}],
                                         tools=tools, max_steps=4)]
    check("an endless tool loop is stopped by the cap",
          events[-1].get("stop_reason") == "max_steps", str(events[-1]))
    check("the cap is the number of model turns, not tool calls",
          len(looper.seen) == 4, str(len(looper.seen)))
    check("the user is told why it stopped",
          any("step cap" in e.get("text", "") for e in events
              if e["type"] == "notice"))

    # -- a provider error ends the run rather than looping ------------------
    broken = ScriptedProvider([[{"type": "error", "text": "boom"}]])
    events = [e async for e in agent.run(broken, "m",
                                         [{"role": "user", "content": "x"}],
                                         tools=tools)]
    check("a provider error stops the loop",
          events[-1].get("stop_reason") == "error" and len(broken.seen) == 1)

    # -- providers without tool support are not handed tools ---------------
    class NoTools(ScriptedProvider):
        supports_tools = False

    plain = NoTools([[{"type": "delta", "text": "hello"}, {"type": "done"}]])
    _ = [e async for e in agent.run(plain, "m",
                                    [{"role": "user", "content": "x"}], tools=tools)]
    check("a provider that cannot use tools is offered none",
          plain.tools_offered == [])

    await registry.disable("notes")
    registry.remove("notes")


# ---------------------------------------------------------------------------
# 6. Message serialisation per provider
# ---------------------------------------------------------------------------

def _serialisation() -> None:
    from aegis.providers.anthropic import to_anthropic_messages
    from aegis.providers.ollama_provider import to_ollama_messages
    from aegis.providers.openai_compat import to_openai_messages

    transcript = [
        {"role": "user", "content": "delete it"},
        {"role": "assistant", "content": "ok",
         "tool_calls": [{"id": "c1", "name": "t", "arguments": {"id": "n1"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "done",
         "images": [("image/png", "AAAA")]},
    ]

    oai = to_openai_messages(transcript)
    assistant = oai[1]
    check("OpenAI: tool_calls serialised with a JSON argument string",
          assistant["tool_calls"][0]["function"]["arguments"] == '{"id": "n1"}',
          str(assistant["tool_calls"][0]["function"]["arguments"]))
    check("OpenAI: tool result uses role=tool with the call id",
          oai[2]["role"] == "tool" and oai[2]["tool_call_id"] == "c1")
    check("OpenAI: a tool image becomes a following user message",
          oai[3]["role"] == "user"
          and oai[3]["content"][1]["type"] == "image_url"
          and oai[3]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    ant = to_anthropic_messages(transcript)
    check("Anthropic: tool_use block built from the call",
          ant[1]["content"][-1]["type"] == "tool_use"
          and ant[1]["content"][-1]["input"] == {"id": "n1"})
    check("Anthropic: tool result is a user message with tool_result",
          ant[2]["role"] == "user"
          and ant[2]["content"][0]["type"] == "tool_result"
          and ant[2]["content"][0]["tool_use_id"] == "c1")
    check("Anthropic: the image stays inside the tool_result",
          ant[2]["content"][0]["content"][1]["type"] == "image")

    merged = to_anthropic_messages([
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": "2"},
    ])
    check("Anthropic: consecutive tool results merge into one user message",
          len(merged) == 1 and len(merged[0]["content"]) == 2, str(len(merged)))

    oll = to_ollama_messages(transcript)
    check("Ollama: tool_calls use the function shape",
          oll[1]["tool_calls"][0]["function"]["arguments"] == {"id": "n1"})
    check("Ollama: images go in a sibling list of bare base64",
          oll[3]["images"] == ["AAAA"])

    from aegis.providers.base import flatten_for_text, parse_arguments
    flat = flatten_for_text(transcript)
    check("CLI flattening keeps the tool result as readable text",
          all(isinstance(m["content"], str) for m in flat)
          and any("done" in m["content"] for m in flat))
    check("malformed tool arguments do not raise",
          "__unparsed__" in parse_arguments('{"broken'))
    check("well-formed arguments parse", parse_arguments('{"a":1}') == {"a": 1})


# ---------------------------------------------------------------------------
# 7. Discovery of other hosts' configs
# ---------------------------------------------------------------------------

def _discovery() -> None:
    from aegis.mcp import discovery

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(
            'model = "gpt-5"\n\n'
            '[mcp_servers.gdrive]\n'
            'command = "npx"\n'
            'args = ["-y", "@modelcontextprotocol/server-gdrive"]\n\n'
            '[mcp_servers.gmail]\n'
            'command = "uvx"\n'
            'args = ["mcp-gmail"]\n'
            '[mcp_servers.gmail.env]\n'
            'GMAIL_TOKEN = "secret-value-do-not-leak"\n',
            encoding="utf-8")

        (home / ".cursor").mkdir()
        (home / ".cursor" / "mcp.json").write_text(json.dumps({
            "mcpServers": {"remote": {"url": "https://example.com/mcp"}}}),
            encoding="utf-8")

        original_home = Path.home
        Path.home = staticmethod(lambda: home)          # type: ignore[assignment]
        try:
            codex = discovery.from_codex()
            names = {d.name for d in codex}
            check("Codex config.toml parsed", names == {"gdrive", "gmail"},
                  str(sorted(names)))
            gdrive = next(d for d in codex if d.name == "gdrive")
            check("command and args read from TOML",
                  gdrive.spec["command"] == "npx"
                  and gdrive.spec["args"][1] == "@modelcontextprotocol/server-gdrive")
            gmail = next(d for d in codex if d.name == "gmail")
            check("env is kept in the spec for connecting",
                  gmail.spec["env"]["GMAIL_TOKEN"] == "secret-value-do-not-leak")
            check("env VALUES are never sent to the UI",
                  "secret-value-do-not-leak" not in json.dumps(gmail.to_dict()),
                  json.dumps(gmail.to_dict())[:120])
            check("env KEY names are still listed",
                  gmail.to_dict()["env_keys"] == ["GMAIL_TOKEN"])

            cursor = discovery.from_cursor()
            check("remote URL servers discovered",
                  len(cursor) == 1 and cursor[0].transport == "http",
                  str([c.transport for c in cursor]))

            report = discovery.scan_report()
            check("scan merges every source",
                  report["by_origin"].get("codex") == 2
                  and report["by_origin"].get("cursor") == 1,
                  str(report["by_origin"]))
        finally:
            Path.home = original_home                   # type: ignore[assignment]

    # JSONC: VS Code allows comments and trailing commas, JSON does not.
    cleaned = discovery._strip_jsonc(
        '{\n  // a comment\n  "a": 1, /* block */\n  "b": "keep // this",\n}')
    check("JSONC comments and trailing commas stripped",
          json.loads(cleaned) == {"a": 1, "b": "keep // this"}, cleaned)

    check("a malformed config is reported, not fatal",
          discovery._normalise("x", {"nonsense": True}, "test", Path(".")) is None)


# ---------------------------------------------------------------------------
# 8. Computer use
# ---------------------------------------------------------------------------

def _computer() -> None:
    from aegis.config import settings
    from aegis.tools import computer
    from aegis.tools.base import READ, WRITE

    settings.set("computer_use_enabled", False)
    check("switching computer use off removes its tools", computer.tools() == [])

    settings.set("computer_use_enabled", True)
    cap = computer.capability()
    tools = computer.tools()
    if not cap.available:
        check("unavailable computer use exposes no tools and says why",
              tools == [] and bool(cap.detail), cap.detail)
    else:
        names = {t.raw_name for t in tools}
        check("computer tool set present",
              {"screenshot", "click", "type", "key"} <= names, str(sorted(names)))
        risks = {t.raw_name: t.risk for t in tools}
        check("screenshot is a read", risks.get("screenshot") == READ)
        check("click is a write", risks.get("click") == WRITE)
        check("type is a write", risks.get("type") == WRITE)

    # Coordinate mapping is pure arithmetic and testable without a display.
    computer._state.update({"scale": 0.5, "screen": (2560, 1440), "shot": (1280, 720)})
    check("image coordinates scale back to screen pixels",
          computer._to_screen(100, 200) == (200, 400),
          str(computer._to_screen(100, 200)))
    check("out-of-range coordinates are clamped on-screen",
          computer._to_screen(99999, 99999) == (2559, 1439),
          str(computer._to_screen(99999, 99999)))
    computer._state.update({"scale": 1.0, "screen": (1920, 1080)})
    check("unscaled screenshots map 1:1", computer._to_screen(640, 480) == (640, 480))

    settings.set("computer_use_enabled", True)     # restore the shipped default


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("MCP protocol (real server process)")
    asyncio.run(_mcp_roundtrip())

    section("MCP registry and catalogue")
    asyncio.run(_registry())

    section("tool classification")
    _classification()

    section("approval gate")
    asyncio.run(_approvals())

    section("agent loop")
    asyncio.run(_agent_loop())

    section("provider message serialisation")
    _serialisation()

    section("config discovery")
    _discovery()

    section("computer use")
    _computer()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-mcp-test-"))
    from harness import report
    run_all()
    sys.exit(report())
