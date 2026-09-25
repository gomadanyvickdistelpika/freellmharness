"""Skills, self-authoring, memory, the vault index, subagents and browser tools.

The important tests here are the gates: a skill the agent writes must be inert
until approved, a subagent must not be able to spawn another, and nothing in the
memory layer may write to the vault.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section  # noqa: E402


class ScriptedProvider:
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
        for event in self.turns[min(len(self.seen) - 1, len(self.turns) - 1)]:
            yield event


# ---------------------------------------------------------------------------
# 1. Skills
# ---------------------------------------------------------------------------

def _skills() -> None:
    from aegis import skills

    meta, body = skills.parse_frontmatter(
        "---\nname: demo\ndescription: >\n  line one\n  line two\n---\n\n# Body\ntext\n")
    check("frontmatter name parsed", meta.get("name") == "demo")
    check("folded description is joined onto one line",
          meta.get("description") == "line one line two", repr(meta.get("description")))
    check("body starts after the frontmatter", body.startswith("# Body"), body[:20])

    meta, body = skills.parse_frontmatter("no frontmatter here")
    check("a file with no frontmatter is all body",
          meta == {} and body == "no frontmatter here")
    meta, body = skills.parse_frontmatter("---\nname: x\nunterminated")
    check("unterminated frontmatter does not raise", isinstance(body, str))

    reg = skills.registry()
    live = skills.loadable()
    check("the bundled pack is found", len(live) >= 12, str(len(live)))
    names = {s.name for s in live}
    check("the profile skill is present", "my-profile" in names)
    check("the VMware skill is present", "vmware-support" in names)
    check("every bundled skill has a description",
          all(s.description for s in live if s.origin == "bundled"))
    check("every bundled skill has a body",
          all(len(s.body) > 200 for s in live if s.origin == "bundled"))

    index = skills.index_text()
    check("the router index names every skill",
          all(s.name in index for s in live), str(len(index)))
    check("the index carries descriptions, not bodies",
          len(index) < sum(len(s.body) for s in live) / 3,
          f"index {len(index)} chars")

    loaded = skills.get("vmware-support")
    check("a skill loads by name", loaded is not None and "vCenter" in loaded.body)
    check("an unknown skill returns nothing", skills.get("nope") is None)


def _self_authoring() -> None:
    """A skill the agent writes must do nothing until a human approves it."""
    from aegis import skills

    before = {s.name for s in skills.loadable()}
    result = skills.draft("auto-written", "Use this when testing self-authoring.",
                          "# Instructions\nAlways say banana.")
    check("the agent can draft a skill", result["ok"] is True, str(result))
    check("the draft reports that it is pending",
          result["status"] == "pending_approval")
    check("the message tells the agent to say it is waiting",
          "approve" in result["message"].lower())

    after = {s.name for s in skills.loadable()}
    check("a drafted skill is NOT loadable", before == after, str(after - before))
    check("a drafted skill is NOT in the router index",
          "auto-written" not in skills.index_text())
    check("skills.get refuses a pending skill", skills.get("auto-written") is None)
    check("it does appear in the pending list",
          any(s.name == "auto-written" for s in skills.pending()))

    check("a bad name is refused",
          skills.draft("Bad Name!", "d", "b")["ok"] is False)
    check("an empty body is refused",
          skills.draft("ok-name", "d", "")["ok"] is False)
    check("an oversized skill is refused",
          skills.draft("big-one", "d", "x" * (skills.MAX_SKILL_BYTES + 10))["ok"] is False)

    # The tool the agent calls must have no route to approval.
    tool_names = {t.raw_name for t in skills.tools()}
    check("the agent has a write tool but no approve tool",
          "write" in tool_names and not {"approve", "reject"} & tool_names,
          str(sorted(tool_names)))

    approved = skills.approve("auto-written")
    check("a human can approve it", approved["ok"] is True)
    check("once approved it is loadable", skills.get("auto-written") is not None)
    check("once approved it joins the index",
          "auto-written" in skills.index_text())

    skills.draft("to-be-rejected", "d", "body")
    check("rejecting removes it", skills.reject("to-be-rejected")["ok"] is True)
    check("a rejected skill is gone",
          not any(s.name == "to-be-rejected" for s in skills.pending()))
    check("rejecting something that is not there fails cleanly",
          skills.reject("never-existed")["ok"] is False)

    check("a user skill can be deleted",
          skills.delete_user_skill("auto-written")["ok"] is True)
    check("a bundled skill cannot be deleted",
          skills.delete_user_skill("vmware-support")["ok"] is False)
    check("the bundled skill survived that attempt",
          skills.get("vmware-support") is not None)


# ---------------------------------------------------------------------------
# 2. Memory and the vault
# ---------------------------------------------------------------------------

def _memory() -> None:
    from aegis import memory

    fact_id = memory.save_fact("Alex runs AEGIS on a laptop with 16 GB",
                               tags="hardware")
    check("a fact is saved", fact_id > 0)
    hits = memory.search_facts("laptop")
    check("a fact is found by search", any(h["id"] == fact_id for h in hits),
          str(len(hits)))
    check("tags are searchable",
          any(h["id"] == fact_id for h in memory.search_facts("", "hardware")))
    check("nonsense search returns nothing", memory.search_facts("zzzqqq") == [])
    check("empty text is not saved", memory.save_fact("   ") == 0)
    check("a fact can be forgotten", memory.forget_fact(fact_id) is True)
    check("forgetting twice is not an error", memory.forget_fact(fact_id) is False)


def _vault() -> None:
    from aegis import memory
    from aegis.config import settings

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "cases").mkdir()
        (root / "cases" / "psod.md").write_text(
            "# PSOD triage\nPurple screen on ESXi 8.0, check the vmkernel log.\n",
            encoding="utf-8")
        (root / "career.md").write_text(
            "# Career\nExampleCorp interview went well.\n", encoding="utf-8")
        (root / ".obsidian").mkdir()
        (root / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
        (root / "huge.md").write_text("x" * (memory.MAX_NOTE_BYTES + 100),
                                      encoding="utf-8")

        settings.set("vault_path", str(root))
        result = memory.reindex_vault()
        check("the vault indexes", result["ok"] is True, str(result))
        check("both real notes are indexed", result["total"] == 2, str(result))
        check("the .obsidian folder is skipped", result["total"] == 2)
        check("an oversized file is skipped, not indexed", result["skipped"] == 1,
              str(result["skipped"]))

        hits = memory.search_vault("vmkernel")
        check("vault search finds a note", len(hits) == 1, str(hits))
        check("the hit carries its path", hits[0]["path"] == "cases/psod.md",
              hits[0]["path"])
        check("the title comes from the heading", hits[0]["title"] == "PSOD triage")

        note = memory.read_vault_note("cases/psod.md")
        check("a note reads back", note["ok"] and "Purple screen" in note["content"])
        fuzzy = memory.read_vault_note("psod")
        check("a partial path still finds the note", fuzzy["ok"] is True)
        check("an unknown note fails with advice",
              memory.read_vault_note("nope.md")["ok"] is False)

        # Nothing may write into the vault.
        (root / "career.md").write_text("# Career\nChanged.\n", encoding="utf-8")
        again = memory.reindex_vault()
        check("a changed note is re-indexed", again["updated"] == 1, str(again))
        check("reindexing did not add files to the vault",
              len(list(root.rglob("*.md"))) == 3, str(len(list(root.rglob("*.md")))))

        (root / "career.md").unlink()
        gone = memory.reindex_vault()
        check("a deleted note leaves the index", gone["removed"] == 1, str(gone))

        stats = memory.vault_stats()
        check("stats report the vault", stats["notes"] == 1, str(stats["notes"]))
        check("stats state plainly that nothing writes to the vault",
              stats["writes_to_vault"] is False)

        source = Path(memory.__file__).read_text("utf-8")
        check("the memory module contains no vault write call",
              "write_text" not in source and "open(" not in source,
              "found a write primitive in memory.py")

    settings.set("vault_path", "")


# ---------------------------------------------------------------------------
# 3. Agents and subagents
# ---------------------------------------------------------------------------

async def _agents() -> None:
    from aegis import agents
    from aegis.config import settings
    from aegis.tools import catalogue

    roles = agents.roles()
    check("every skill is available as a role", len(roles) >= 12, str(len(roles)))
    check("the VMware role exists", "vmware-support" in roles)

    prompt, resolved = agents.build_prompt(role="vmware-support")
    check("the role's skill is in the prompt", "vCenter" in prompt)
    check("the base profile is prepended", "Ground rules" in prompt)
    check("the resolved role is reported", resolved == "vmware-support")
    check("the prompt says it cannot nest",
          "cannot spawn" in prompt.lower(), prompt[-200:])

    prompt, resolved = agents.build_prompt(instructions="Be terse.")
    check("custom instructions work without a role",
          "Be terse." in prompt and resolved == "")
    prompt, _ = agents.build_prompt(role="does-not-exist")
    check("an unknown role degrades to the base prompt", "Ground rules" in prompt)

    # A real subagent run against a scripted provider.
    provider = ScriptedProvider([
        [{"type": "delta", "text": "The answer is 42."}, {"type": "done"}]])
    result = await agents.run_subagent("what is the answer", role="vmware-support",
                                       provider=provider, model="m")
    check("a subagent runs and returns text", result["ok"] and "42" in result["text"])
    check("the subagent got its own system prompt",
          provider.seen[0][0]["role"] == "system" and "vCenter" in provider.seen[0][0]["content"])
    check("the subagent got only the task, not the parent conversation",
          len(provider.seen[0]) == 2, str(len(provider.seen[0])))
    check("the subagent is NOT given a spawn tool",
          not any(t.name == "agent__spawn" for t in provider.tools_offered),
          str([t.name for t in provider.tools_offered][:5]))

    # The spawn tool itself, as the parent would see it.
    spawn = agents.tools(provider, "m")
    check("the spawn tool exists (plus v2's parallel spawn)",
          len(spawn) == 2 and spawn[0].name == "agent__spawn"
          and spawn[1].name == "agent__spawn_parallel")
    check("spawn is classified as a write", spawn[0].risk == "write")
    check("the tool description lists the roles",
          "vmware-support" in spawn[0].description)

    events: list[dict[str, Any]] = []
    provider2 = ScriptedProvider([
        [{"type": "delta", "text": "Done looking."}, {"type": "done"}]])
    spawn2 = agents.tools(provider2, "m", on_event=events.append)
    out = await spawn2[0].handler({"task": "look something up", "role": "job-search"})
    check("the spawn tool returns the subagent's answer",
          "Done looking." in out["text"], out["text"][:80])
    check("the result is labelled as coming from a subagent",
          out["text"].startswith("[subagent"), out["text"][:40])
    empty = await spawn2[0].handler({"task": "   "})
    check("spawn with no task is refused", empty.get("error") is True)

    # The main catalogue must never contain spawn - that is what stops nesting.
    check("the plain catalogue has no spawn tool",
          not any(t.name == "agent__spawn" for t in catalogue()))

    settings.set("subagents_enabled", False)
    check("subagents can be switched off", agents.tools(provider, "m") == [])
    settings.set("subagents_enabled", True)


# ---------------------------------------------------------------------------
# 4. Browser tools
# ---------------------------------------------------------------------------

def _browser() -> None:
    from aegis.config import settings
    from aegis.tools import browser
    from aegis.tools.base import READ, WRITE

    ok, hint = browser.available()
    settings.set("browser_use_enabled", False)
    check("browser use off means no tools", browser.tools() == [])

    settings.set("browser_use_enabled", True)
    tools = browser.tools()
    if not ok:
        check("without playwright the tools are absent and a hint is given",
              tools == [] and "playwright" in hint.lower(), hint)
        return

    names = {t.raw_name for t in tools}
    check("the browser tool set is present",
          {"open", "read", "click", "type", "screenshot"} <= names, str(sorted(names)))
    risks = {t.raw_name: t.risk for t in tools}
    check("reading a page is a read", risks.get("read") == READ)
    check("navigation is classified as a read", risks.get("open") == READ)
    check("clicking is a write", risks.get("click") == WRITE)
    check("typing is a write", risks.get("type") == WRITE)
    check("click takes a ref number, not a selector",
          "ref" in tools[0].input_schema.get("properties", {})
          or any("ref" in t.input_schema.get("properties", {})
                 for t in tools if t.raw_name == "click"))


# ---------------------------------------------------------------------------
# 5. Wiring: the catalogue and the API
# ---------------------------------------------------------------------------

def _wiring() -> None:
    from aegis.tools import catalogue, summary

    servers = {t.server for t in catalogue()}
    check("memory tools are in the catalogue", "memory" in servers, str(servers))
    check("skill tools are in the catalogue", "skills" in servers)

    names = {t.name for t in catalogue()}
    check("skill__load is offered", "skill__load" in names)
    check("memory__vault_search is offered", "memory__vault_search" in names)
    check("every tool name fits the API limit",
          all(len(n) <= 64 for n in names),
          str([n for n in names if len(n) > 64]))
    check("every tool name is API-legal",
          all(n.replace("_", "").replace("-", "").isalnum() for n in names))

    data = summary()
    check("the summary counts read and write separately",
          data["read"] + data["write"] == data["total"])


def _api() -> None:
    from fastapi.testclient import TestClient
    from aegis.server import app

    with TestClient(app) as client:
        r = client.get("/api/skills")
        check("GET /api/skills", r.status_code == 200
              and r.json()["counts"]["total"] >= 12)

        r = client.get("/api/skills/vmware-support")
        check("a skill body can be fetched",
              r.status_code == 200 and len(r.json()["skill"]["body"]) > 200)
        check("an unknown skill 404s",
              client.get("/api/skills/nope").status_code == 404)

        r = client.get("/api/agents")
        check("GET /api/agents", r.status_code == 200 and r.json()["nesting"] is False)

        r = client.get("/api/memory")
        check("GET /api/memory", r.status_code == 200 and "stats" in r.json())
        r = client.post("/api/memory", json={"text": "via the api", "tags": "t"})
        fact_id = r.json()["id"]
        check("a fact can be saved over the API", r.json()["ok"] is True)
        check("it can be deleted again",
              client.delete(f"/api/memory/{fact_id}").json()["ok"] is True)

        r = client.get("/api/vault")
        check("GET /api/vault", r.status_code == 200
              and r.json()["writes_to_vault"] is False)
        r = client.post("/api/vault/reindex", json={})
        check("reindexing with no vault set fails with advice",
              r.json()["ok"] is False and "vault" in r.json()["error"].lower())

        r = client.get("/api/browser")
        check("GET /api/browser", r.status_code == 200 and "mode" in r.json())

        r = client.get("/api/computer")
        check("computer use ships switched on", r.json()["enabled"] is True)


# ---------------------------------------------------------------------------

def run_all() -> None:
    section("skills")
    _skills()
    section("self-authored skills are gated")
    _self_authoring()
    section("agent memory")
    _memory()
    section("vault index (read-only)")
    _vault()
    section("agents and subagents")
    asyncio.run(_agents())
    section("browser tools")
    _browser()
    section("wiring")
    _wiring()
    section("platform API")
    _api()


if __name__ == "__main__":
    os.environ.setdefault("AEGIS_DATA_DIR",
                          tempfile.mkdtemp(prefix="aegis-platform-test-"))
    from harness import report
    run_all()
    sys.exit(report())
