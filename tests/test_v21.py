"""AEGIS v2.1: personal knowledge tiers, text-tool protocol, Boost, Codex-style
coding safety (diffs, checkpoints, read-only git), browser take-over, your
agents, the MCP catalogue, voice transcription, multi-key rotation, uploads
into the workspace and fenced downloads."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


def serve(handler) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Scripted:
    """A provider whose successive calls follow scripts."""
    kind = "api"
    supports_tools = True
    supports_images = True
    blurb = needs = ""

    def __init__(self, key, scripts, supports_tools=True):
        self.key = self.label = key
        self.scripts = list(scripts)
        self.supports_tools = supports_tools
        self.calls = []

    async def status(self): return {"ready": True, "detail": "", "hint": ""}
    async def models(self): return ["m"]
    def describe(self): return {"key": self.key}

    async def chat(self, messages, model, **opts):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": opts.get("tools"), "model": model})
        script = self.scripts.pop(0) if self.scripts else [{"type": "delta", "text": "…"}]
        for text in script if isinstance(script, list) else [script]:
            yield text if isinstance(text, dict) else {"type": "delta", "text": text}
        yield {"type": "done"}


# ---------------------------------------------------------------------------

async def _personal() -> None:
    from aegis import personal, skills, superagent
    from aegis.config import settings

    entries = {e["name"]: e for e in personal.entries()}
    check("a fresh install starts with templates, not a person",
          personal.setup_needed() and "{{" in entries["about"]["body"])
    check("an unfilled template never reaches the prompt",
          "{{" not in personal.prompt_block())
    import demo_profile
    demo_profile.fill()
    check("filling in 'about' ends first-run setup", not personal.setup_needed())
    entries = {e["name"]: e for e in personal.entries()}
    check("the knowledge pack is seeded", {"about", "career", "job-search", "family"}
          <= set(entries), str(sorted(entries)))
    check("family is private", entries["family"]["tier"] == "private")
    check("job search is personal", entries["job-search"]["tier"] == "personal")
    check("working style is public", entries["working-style"]["tier"] == "public")

    settings.set("profile_sharing", "lite")
    cloud = superagent.system_prompt([{"role": "user", "content": "help my son with homework"}], tools=[])
    check("a cloud prompt carries the public profile", "Alex Example" in cloud)
    check("...but no children's names", "Robin" not in cloud, cloud[:200])
    check("...and a private specialist is only announced",
          "Specialist available: family-and-school" in cloud
          and "Specialist loaded: family-and-school" not in cloud)
    local = superagent.system_prompt([{"role": "user", "content": "help my son with homework"}],
                                     tools=[], local_model=True)
    check("a local model gets the private specialist", "Specialist loaded: family-and-school" in local)

    personal.set_local(False)
    load = next(t for t in skills.tools() if t.name == "skill__load")
    res = await load.handler({"name": "my-profile"})
    check("a cloud model cannot load my-profile silently", res.get("error")
          and "me__private" in res["text"])
    tools = {t.name: t for t in personal.tools()}
    check("me__private asks first on a cloud model", tools["me__private"].risk == "write")
    res = await tools["me__about"].handler({"topic": "job search salary"})
    check("me__about finds job-search rules", "40,000" in res["text"])
    res = await tools["me__about"].handler({"topic": "family children"})
    check("me__about never returns private notes", "Robin" not in res["text"])
    personal.set_local(True)
    tools = {t.name: t for t in personal.tools()}
    check("me__private runs freely for a local model", tools["me__private"].risk == "read")
    res = await tools["me__private"].handler({"topic": "family", "reason": "tutor"})
    check("...and returns the family note", "Robin" in res["text"])
    personal.set_local(False)

    saved = personal.save("goals", "---\ntitle: Goals\ntier: public\ntopics: goals\n---\nShip AEGIS.")
    check("notes can be added", saved["ok"] and personal.get("goals")["tier"] == "public")
    personal.save("career", personal.raw("career") + "\nEDITED")
    personal.ensure()
    check("your edits survive a restart (never overwritten)", "EDITED" in personal.raw("career"))


async def _text_tools() -> None:
    from aegis.providers.texttools import TextToolsProvider, parse_calls

    prose, calls = parse_calls('Let me look.\n<tool_call>{"name": "web__search", '
                               '"arguments": {"query": "vcf 9",}}</tool_call>')
    check("text tool calls are parsed (even with a trailing comma)",
          calls and calls[0]["name"] == "web__search" and calls[0]["arguments"]["query"] == "vcf 9",
          str(calls))
    check("...and the prose is kept", prose.strip() == "Let me look.")
    _, calls = parse_calls('<tool_call>```json\n{"name":"a","parameters":{"x":1}}\n```')
    check("fenced JSON, 'parameters' and a missing close tag are tolerated",
          calls and calls[0]["arguments"] == {"x": 1})

    class T:
        name = "web__search"
        description = "search the web"
        input_schema = {"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]}

    inner = Scripted("x", [["I will search. <tool", "_call>{\"name\": \"web__search\", "
                            "\"arguments\": {\"query\": \"springfield\"}}</tool_call>"]],
                     supports_tools=False)
    wrapped = TextToolsProvider(inner)
    events = [e async for e in wrapped.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
         {"role": "tool", "name": "x", "tool_call_id": "1", "content": "r"}], "m", tools=[T()])]
    text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
    calls = [e for e in events if e["type"] == "tool_calls"]
    check("a text-only model can call tools", calls and calls[0]["calls"][0]["name"] == "web__search")
    check("...the markup never reaches the user, even split across chunks",
          "<tool" not in text and text.strip() == "I will search.", repr(text))
    sent = inner.calls[0]
    check("...the protocol and tool list are in the system prompt",
          "web__search(query: string)" in sent["messages"][0]["content"])
    check("...tool history is flattened to text", all(m["role"] != "tool" for m in sent["messages"]))
    check("...and no native tools are sent", sent["tools"] is None)

    from aegis import agent
    loop_provider = Scripted("y", [['<tool_call>{"name": "plan__update", "arguments": '
                                    '{"items": [{"step": "one"}]}}</tool_call>'], ["All done."]],
                             supports_tools=False)
    from aegis.boost import plan_tools
    events = [e async for e in agent.run(loop_provider, "m", [{"role": "user", "content": "go"}],
                                         tools=plan_tools())]
    results = [e for e in events if e["type"] == "tool_result"]
    check("the agent loop runs a text-protocol tool end to end",
          results and results[0]["ok"] and "[ ] one" in results[0]["preview"], str(results)[:200])


async def _boost_and_plan() -> None:
    from aegis import boost

    check("boost is auto for code", boost.applies("code", [], "fix this python function please"))
    check("boost skips small talk", not boost.applies("quick", [], "hi there"))
    check("boost follows job-search", boost.applies("chat", ["job-search"],
                                                    "write my cover letter for Huntress"))
    check("boost can be forced off", not boost.applies("code", [], "fix my code", "off"))
    check("LGTM is clean", boost.is_clean("LGTM") and boost.is_clean("**LGTM**"))
    check("a list of problems is not", not boost.is_clean("- The command is wrong"))
    plan = boost.plan_tools()[0]
    out = await plan.handler({"items": [{"step": "Read repo", "status": "done"},
                                        {"step": "Fix bug", "status": "in_progress"}]})
    check("the plan tool renders a checklist", "[x] Read repo" in out["text"]
          and "[~] Fix bug" in out["text"])


def _boost_end_to_end() -> None:
    from fastapi.testclient import TestClient

    from aegis import providers as provider_mod, router, store
    from aegis.config import settings
    from aegis.server import app

    long_answer = "Here is the PowerShell script:\n" + ("Get-ChildItem | Rename-Item ...\n" * 12)
    answerer = Scripted("a", [[long_answer], ["IMPROVED ANSWER " + "x" * 50]])
    critic = Scripted("b", [["- It deletes files without -WhatIf\n- Missing error handling"]])
    settings.set("routes", {"auto": {"label": "Auto", "allow_paid": True, "smart": False,
                                     "candidates": [{"provider": "a", "model": "m1"},
                                                    {"provider": "b", "model": "m2"}]}})
    settings.set("model_caps", {})
    router.book._data.clear()
    original = provider_mod.registry

    def reg(include_routes=False):
        out = {"a": answerer, "b": critic}
        if include_routes:
            from aegis.providers.routed import route_providers
            out.update({r.key: r for r in route_providers()})
        return out

    provider_mod.registry = reg
    try:
        with TestClient(app) as client:
            r = client.post("/api/chat", json={"provider": "route:auto", "use_tools": False,
                                               "text": "write a powershell script to rename all my files by date"})
            body = r.text
            chat_id = r.headers.get("X-Aegis-Chat-Id")
        check("a hard request is reviewed by the second model", len(critic.calls) == 1,
              str(len(critic.calls)))
        check("...the review names the reviewer", "boost_revision" in body and "b/m2" in body)
        check("...the answer is rewritten", "IMPROVED ANSWER" in body)
        check("...the reviewer saw the question and the draft",
              "rename all my files" in critic.calls[0]["messages"][-1]["content"]
              and "Get-ChildItem" in critic.calls[0]["messages"][-1]["content"])
        final = [m for m in store.transcript_for_model(chat_id) if m["role"] == "assistant"]
        check("...and the stored answer is the revision",
              final and final[-1]["content"].startswith("IMPROVED ANSWER"), str(final)[:200])
        shown = store.transcript_for_display(chat_id)[-1]
        kinds = [p["kind"] for p in shown.get("parts", [])]
        check("...with the draft kept, folded", "draft" in kinds and "review" in kinds, str(kinds))
        check("the plan note is added for boosted runs",
              "demanding request" in answerer.calls[0]["messages"][0]["content"])
    finally:
        provider_mod.registry = original


async def _coding_safety() -> None:
    from aegis import agent, projects
    from aegis.config import settings
    from aegis.tools import code, files as ft

    settings.set("workspace_enabled", True)
    projects.set_current("", "")
    ws = projects.general_workspace()
    target = ws / "app.py"
    target.write_text("print('v1')\n", encoding="utf-8")
    summary = agent._approval_summary("files__edit", {"path": str(target), "old": "v1", "new": "v2"})
    check("the approval card shows a real diff", "-print('v1')" in summary and "+print('v2')" in summary,
          summary)
    summary = agent._approval_summary("code__run", {"command": "pytest -q"})
    check("...and the exact command for code__run", summary.startswith("$ pytest -q"))

    ft._edit(path=str(target), old="v1", new="v2")
    check("the edit happened", "v2" in target.read_text())
    res = ft._undo()
    check("files__undo restores the previous version", res.get("error") is False
          and "v1" in target.read_text(), str(res))
    new_file = ws / "new.txt"
    ft._write(path=str(new_file), content="hello")
    out = ft._undo()
    check("undoing a brand-new file removes it", not new_file.exists(), str(out))
    check("checkpoints are listed", len(ft.checkpoints()) >= 3)
    wrote = ft._write(path=str(ws / "report.md"), content="# hi")
    check("a written file carries a download marker", "[[file:" in wrote["text"])

    res = await code.run_git(args="push origin main")
    check("git push is refused by the read-only git tool", res["error"] and "read-only" in res["text"])
    res = await code.run_git(args="branch -D main")
    check("...and so is deleting a branch", res["error"])
    os.system(f'cd "{ws}" && git init -q 2>/dev/null')
    res = await code.run_git(args="status --short")
    check("read-only git runs", "[exit" in res["text"], res["text"][:120])

    p = projects.save("Repo work")
    pws = projects.workspace(p["key"])
    (pws / "AGENTS.md").write_text("Always run pytest before finishing.", encoding="utf-8")
    check("AGENTS.md is read into the project prompt",
          "Always run pytest" in projects.system_block(p["key"]))


async def _browser_control() -> None:
    from aegis.tools import browser

    browser.set_user_control(True)
    click = browser._wrap(browser._click)
    res = await click({"ref": 1})
    check("while you hold the browser, agent clicks wait", res["error"]
          and "taken control" in res["text"])
    browser.set_user_control(False)
    check("handing back clears the flag", browser.USER_CONTROL["on"] is False)


async def _agents_and_mcp() -> None:
    from aegis import agents, custom_agents
    from aegis.mcp import catalog
    from aegis.mcp.registry import registry

    custom_agents.ensure_presets()
    keys = set(custom_agents.all_agents())
    check("the preset agents are installed once", {"coder", "job-hunter", "vmware-tse",
                                                   "family-tutor"} <= keys, str(keys))
    custom_agents.delete("studio")
    custom_agents.ensure_presets()
    check("...and a deleted preset stays deleted", "studio" not in custom_agents.all_agents())
    saved = custom_agents.save("LinkedIn Writer", instructions="Write posts.", tools=["web", "me"],
                               skills=["job-search"])
    key = saved["key"]
    roles = agents.roles()
    check("your agents are subagent roles", f"agent:{key}" in roles
          and "Write posts." in roles[f"agent:{key}"].prompt)

    class T:
        def __init__(self, server): self.server = server; self.name = server + "__x"
    kept = custom_agents.filter_tools([T("web"), T("code"), T("gmail"), T("me")],
                                      custom_agents.get(key))
    check("an agent only gets its tool families", {t.server for t in kept} == {"web", "me"})
    kept = custom_agents.filter_tools([T("web"), T("gmail")], custom_agents.get("job-hunter"))
    check("'mcp' lets an agent use every MCP server", {t.server for t in kept} == {"web", "gmail"})
    tool = next(t for t in custom_agents.tools() if t.name == "agent__create")
    check("creating agents from chat asks first", tool.risk == "write")

    items = {i["key"]: i for i in catalog.listing()}
    check("the MCP catalogue lists GitHub and Google Workspace",
          {"github", "google-workspace", "filesystem"} <= set(items))
    res = await catalog.install("filesystem", {})
    check("installing without the required folder is refused", not res["ok"] and "Folder" in res["error"])
    res = await catalog.install("github", {"token": "ghp_test"}, connect=False)
    spec = registry.configured()["github"]["spec"]
    check("the token lands in the server's header", res["ok"] and
          spec["headers"]["Authorization"] == "Bearer ghp_test")
    res = catalog.add_custom("my tool", command="python", args=["server.py"])
    check("custom MCP servers can be registered", res["ok"] and "my-tool" in registry.configured())
    check("mcp admin tools that change things ask first",
          all(t.risk == "write" for t in catalog.tools() if t.name != "mcp_admin__catalog"))


async def _voice_and_keys() -> None:
    from aegis import voice, vault
    from aegis.config import settings
    from aegis.providers import custom
    from aegis.providers.openai_compat import _KEY_COOL

    class STT(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):                                  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            STT.auth = self.headers.get("Authorization")
            STT.had_model = b"whisper-large-v3-turbo" in body
            out = json.dumps({"text": "bonjour AEGIS"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv, base = serve(STT)
    try:
        custom.save("groq", "Groq", base + "/v1", free_tier=True)
        custom.set_api_key("groq", "gsk_test")
        res = await voice.transcribe(b"x" * 3000, "speech.webm")
        check("voice is transcribed by the first free backend",
              res["ok"] and res["text"] == "bonjour AEGIS" and res["backend"].startswith("groq"), str(res))
        check("...with the Groq key and whisper model", STT.auth == "Bearer gsk_test" and STT.had_model)
    finally:
        srv.shutdown()

    settings.set("stt_backends", [{"provider": "nobody", "model": "x"}])
    res = await voice.transcribe(b"x" * 3000)
    check("with no backend, the page is told to fall back", not res["ok"] and "nobody" in res["error"])
    settings.set("stt_backends", None)

    class Chat(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        seen: list[str] = []

        def do_POST(self):                                  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            auth = self.headers.get("Authorization", "")
            Chat.seen.append(auth)
            if auth.endswith("spent"):
                out = json.dumps({"error": {"message": "daily limit"}}).encode()
                self.send_response(429)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"from key two"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")

    srv, base = serve(Chat)
    try:
        custom.save("multi", "Multi", base + "/v1", free_tier=True)
        custom.set_api_key("multi", "key-spent")
        custom.set_extra_keys("multi", "key-good")
        check("several keys are stored per provider", custom.all_keys("multi") == ["key-spent", "key-good"])
        _KEY_COOL.clear()
        provider = next(p for p in custom.build() if p.key == "multi")
        events = [e async for e in provider.chat([{"role": "user", "content": "hi"}], "m")]
        text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        check("a spent key rotates to the next key before any model switch",
              text == "from key two" and not any(e["type"] == "error" for e in events), str(events)[:200])
        Chat.seen.clear()
        events = [e async for e in provider.chat([{"role": "user", "content": "hi"}], "m")]
        check("...and the spent key is skipped next time",
              Chat.seen == ["Bearer key-good"], str(Chat.seen))
        summary = next(p for p in custom.summary()["providers"] if p["key"] == "multi")
        check("the provider card reports the key count", summary["key_count"] == 2)
    finally:
        srv.shutdown()
    _ = vault


def _uploads_and_downloads() -> None:
    from fastapi.testclient import TestClient

    from aegis import projects
    from aegis import providers as provider_mod
    from aegis.server import app

    fake = Scripted("fake", [["ok"]])
    original = provider_mod.registry
    provider_mod.registry = lambda include_routes=False: {"fake": fake}
    try:
        with TestClient(app) as client:
            chat = client.post("/api/chats", json={}).json()["chat"]["id"]
            up = client.post(f"/api/chats/{chat}/attachments",
                             files={"uploads": ("song.mp3", b"ID3" + b"\x00" * 500, "audio/mpeg")})
            check("any file type uploads", up.status_code == 200, up.text[:200])
            ids = [a["id"] for a in up.json().get("attachments", up.json().get("items", []))] \
                if isinstance(up.json(), dict) else []
            if not ids and isinstance(up.json(), dict):
                ids = [a.get("id") for a in up.json().get("added", []) if a.get("id")]
            r = client.post("/api/chat", json={"chat_id": chat, "provider": "fake", "model": "m",
                                               "text": "what is this?", "attachment_ids": ids,
                                               "use_tools": False})
            sent = fake.calls[-1]["messages"][-1]["content"]
            dest = projects.general_workspace() / "uploads" / "song.mp3"
            check("an upload is also saved into the workspace", dest.is_file(), sent[:200])
            check("...and the model is told where", str(dest) in sent)
            ok = client.get("/api/files/download", params={"path": str(dest)})
            check("workspace files download", ok.status_code == 200 and ok.content.startswith(b"ID3"))
            bad = client.get("/api/files/download", params={"path": "/etc/passwd"})
            check("files outside the fence do not", bad.status_code == 403)
            listing = client.get("/api/workspace").json()
            check("the workspace listing shows the upload",
                  any(i["name"].endswith("song.mp3") for i in listing["items"]))
            me = client.get("/api/me").json()
            check("the About me API lists tiers", any(e["tier"] == "private" for e in me["entries"]))
            cat = client.get("/api/mcp/catalog").json()
            check("the MCP catalogue API reports prerequisites", "node" in cat["prerequisites"])
            ag = client.get("/api/my-agents").json()
            check("the agents API lists presets", len(ag["presets"]) >= 7)
    finally:
        provider_mod.registry = original


def _big_uploads() -> None:
    import os as _os
    from fastapi.testclient import TestClient

    from aegis import files, projects
    from aegis.config import settings
    from aegis.server import app

    check("the default upload limit is 30 GB", files.max_upload() == 30 * 1024 ** 3)
    with TestClient(app) as client:
        chat = client.post("/api/chats", json={}).json()["chat"]["id"]
        size = files.EXTRACT_LIMIT + 5 * 1024 * 1024          # 261 MB
        big = _os.urandom(1024 * 1024) * (size // (1024 * 1024))
        r = client.put(f"/api/chats/{chat}/upload", params={"name": "bundle.tgz", "size": len(big)},
                       content=big)
        item = r.json()
        check("a file over 256 MB streams in", r.status_code == 200 and item.get("ok"), r.text[:200])
        check("...is stored whole on disk", Path(item["path"]).stat().st_size == len(big))
        check("...and is handed to tools by path, not read into text",
              item["kind"] == "large" and item["chars"] == 0)
        del big
        blocks, _ = files.message_parts([item["id"]], projects.general_workspace() / "uploads")
        check("the model is told the path to work on", item["path"] in blocks or "uploads" in blocks,
              blocks[:300])
        linked = projects.general_workspace() / "uploads" / "bundle.tgz"
        check("the workspace copy is a hard link, not a second copy",
              linked.exists() and _os.stat(linked).st_ino == _os.stat(item["path"]).st_ino)
        limit = client.get("/api/upload-limit").json()
        check("the page can ask for the limit and free space",
              limit["max_bytes"] == 30 * 1024 ** 3 and limit["free_bytes"] > 0)
        settings.set("max_upload_gb", 0.0001)                 # ~107 KB
        r = client.put(f"/api/chats/{chat}/upload", params={"name": "x.bin", "size": 500_000},
                       content=b"0" * 500_000)
        check("a file over the limit is refused with a reason", r.status_code == 413
              and "limit" in r.json()["error"])
        r = client.put(f"/api/chats/{chat}/upload", params={"name": "y.bin"},
                       content=b"0" * 500_000)
        check("...even when the size was not declared up front", r.status_code == 413)
        settings.set("max_upload_gb", 30)
        r = client.put(f"/api/chats/{chat}/upload", params={"name": "notes.md"},
                       content=b"# hello\nsmall file")
        check("small files are still read and put in the message",
              r.json().get("inline") in (1, True), r.text[:200])


def run_all() -> None:
    section("what AEGIS knows about you, and who may see it")
    asyncio.run(_personal())
    section("tools for text-only models")
    asyncio.run(_text_tools())
    section("Boost and the plan tool")
    asyncio.run(_boost_and_plan())
    _boost_end_to_end()
    section("Codex-style coding safety")
    asyncio.run(_coding_safety())
    section("browser take-over")
    asyncio.run(_browser_control())
    section("your agents and the MCP catalogue")
    asyncio.run(_agents_and_mcp())
    section("voice and multi-key rotation")
    asyncio.run(_voice_and_keys())
    section("uploads and downloads")
    _uploads_and_downloads()
    section("big uploads, up to 30 GB")
    _big_uploads()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-v21-test-"))
    from harness import report
    run_all()
    sys.exit(report())
