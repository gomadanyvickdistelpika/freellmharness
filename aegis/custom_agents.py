"""Your own agents: a name, instructions, skills, tools and a model route.

An agent is a saved way of working. Pick it in the chat bar and every message
in that chat runs as that agent; name it in a scheduled task and the task runs
as it; the Super Agent can hand work to it as a subagent role.

    instructions   its standing system prompt
    skills         specialists always loaded (on top of what the Super Agent
                   picks per message)
    tools          which tool families it may use - files, code, web, browser,
                   computer, media, me, project, plan, agent, memory, skill -
                   plus "mcp" for every connected MCP server, or a specific
                   server name. Empty means everything.
    route          a route to use when the chat bar does not choose one
    boost          auto / always / off

Seven starting points ship as presets (Coder, Job Hunter, VMware TSE,
Researcher, Studio, Family Tutor, Daily Planner). Add one, then edit it. The
agent itself can create agents from chat (agent__create), behind approval.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .config import settings

TOOL_FAMILIES = ["files", "code", "web", "browser", "computer", "media", "me",
                 "project", "plan", "agent", "memory", "skill", "mcp_admin", "mcp"]
BUILTIN_SERVERS = {"files", "code", "web", "browser", "computer", "media", "me",
                   "project", "plan", "agent", "memory", "skill", "mcp_admin",
                   "skills"}

PRESETS: list[dict[str, Any]] = [
    {"key": "coder", "name": "Coder (Codex-style)", "icon": "⌨",
     "description": "Builds and fixes code in a project workspace: reads the repo, plans, "
                    "edits with diffs you approve, runs tests, uses git.",
     "skills": ["coding", "building-and-projects"],
     "tools": ["files", "code", "web", "plan", "me", "skill", "agent", "mcp"],
     "boost": "always",
     "instructions": (
         "You are a senior software engineer working in the user's workspace, like Codex "
         "or Claude Code. Always: 1) explore first (files__list, code__git status, "
         "files__search, read AGENTS.md/README), 2) write a plan with plan__update, "
         "3) make small edits with files__edit and show what changed, 4) run the code "
         "or tests with code__run / code__python and fix failures, 5) finish with a "
         "summary: files changed, how you verified, how to run it, what is left. "
         "Windows + PowerShell first. Never delete or force-push without asking. "
         "Push every build to a runnable, packaged state (run.bat / README).")},
    {"key": "job-hunter", "name": "Job Hunter", "icon": "💼",
     "description": "Finds live roles that match your About me rules and builds tailored "
                    "CV + cover letter packs.",
     "skills": ["job-search", "research"],
     "tools": ["web", "browser", "files", "me", "plan", "project", "media", "mcp"],
     "boost": "always",
     "instructions": (
         "Load me__about('job-search') and me__about('career') before anything. "
         "If they are empty templates, ask for the CV and target role first. "
         "Follow every standing rule there exactly, verify each role on the employer's "
         "own site, give honest counts, one honest gap paragraph per cover letter, "
         "never invent experience. Output per role: link, why it fits, gaps, tailored CV "
         "bullets, cover letter, and an APPLY-TODAY summary saved to the workspace.")},
    {"key": "vmware-tse", "name": "VMware TSE", "icon": "🖥",
     "description": "Triage VMware/Broadcom issues from logs, find the right KB, draft "
                    "customer updates and escalations.",
     "skills": ["vmware-support", "research"],
     "tools": ["web", "browser", "files", "code", "me", "plan"],
     "boost": "always",
     "instructions": (
         "Work like a Broadcom L3 TSE: timeline, signature, root cause, 1-2 matching "
         "KBs verified on knowledge.broadcom.com, remediation with rollback, then the "
         "customer update. Never invent KB or build numbers. Never send customer "
         "logs or names to a cloud model without asking.")},
    {"key": "researcher", "name": "Researcher", "icon": "🔎",
     "description": "Deep research with sources: searches, reads pages, cross-checks, cites.",
     "skills": ["research"], "tools": ["web", "browser", "files", "plan", "me"],
     "boost": "auto",
     "instructions": ("Search several angles, read the sources (not just snippets), "
                      "cross-check key facts, answer first, then details, and end with "
                      "Sources as markdown links. Use the user's country by default.")},
    {"key": "studio", "name": "Studio", "icon": "🎨",
     "description": "Images, thumbnails, voice-overs, songs (Suno packs), videos.",
     "skills": ["media-studio", "creative-music"], "tools": ["media", "files", "web", "me"],
     "boost": "off",
     "instructions": ("Create original work only — styles, never named artists or known "
                      "characters. Offer 2-4 image variations when exploring. For songs "
                      "write original lyrics in the user's language.")},
    {"key": "family-tutor", "name": "Family Tutor", "icon": "📚",
     "description": "Calm, structured, visual learning help for children at any school level.",
     "skills": ["family-and-school"], "tools": ["media", "files", "me", "plan"],
     "boost": "off",
     "instructions": (
         "Teach in short, calm, predictable steps with lots of praise and visuals; one "
         "idea at a time; check understanding with one small question. For exam "
         "practice, use low-pressure timed chunks. Use the child's favourite themes "
         "(ask, or check me__private('family')) when making worksheets or images. Personal details come from me__private — prefer a "
         "local model for this agent.")},
    {"key": "daily-planner", "name": "Daily Planner", "icon": "🗓",
     "description": "Plans the day and week, sets up scheduled tasks and reminders, keeps "
                    "project notes current.",
     "skills": ["planner"], "tools": ["project", "plan", "web", "me", "files", "mcp"],
     "boost": "auto",
     "instructions": ("Keep plans realistic for the time the user actually has. "
                      "Prioritise hard deadlines first, then commitments, then projects. Offer to turn recurring work into scheduled "
                      "tasks with project__schedule.")},
]


def all_agents() -> dict[str, dict[str, Any]]:
    return dict(settings.get("custom_agents") or {})


def get(key: str) -> dict[str, Any] | None:
    item = all_agents().get(key or "")
    return {**item, "key": key} if item else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.strip().lower())[:40].strip("-")


def save(name: str, *, key: str = "", instructions: str = "", description: str = "",
         skills: list[str] | None = None, tools: list[str] | None = None,
         route: str = "", boost: str = "auto", icon: str = "") -> dict[str, Any]:
    if not name.strip():
        return {"ok": False, "error": "An agent needs a name."}
    stored = all_agents()
    if not key:
        key = _slug(name) or f"agent-{int(time.time())}"
        if key in stored:
            key = f"{key}-{int(time.time()) % 10000}"
    stored[key] = {
        "name": name.strip()[:60], "description": description.strip()[:400],
        "instructions": instructions.strip()[:20000],
        "skills": [s for s in (skills or []) if s][:8],
        "tools": [t for t in (tools or []) if t][:20],
        "route": route, "boost": boost if boost in ("auto", "always", "off") else "auto",
        "icon": icon[:4], "updated": time.time(),
    }
    settings.set("custom_agents", stored)
    return {"ok": True, "key": key, "agent": get(key)}


def add_preset(key: str) -> dict[str, Any]:
    preset = next((p for p in PRESETS if p["key"] == key), None)
    if not preset:
        return {"ok": False, "error": f"No preset {key!r}."}
    return save(preset["name"], key=preset["key"] if preset["key"] not in all_agents() else "",
                instructions=preset["instructions"], description=preset["description"],
                skills=preset["skills"], tools=preset["tools"], boost=preset["boost"],
                icon=preset.get("icon", ""))


def ensure_presets() -> None:
    """First run: add the presets once so they are there to pick."""
    if settings.get("agent_presets_seeded"):
        return
    for preset in PRESETS:
        if preset["key"] not in all_agents():
            add_preset(preset["key"])
    settings.set("agent_presets_seeded", True)


def delete(key: str) -> bool:
    stored = all_agents()
    existed = key in stored
    stored.pop(key, None)
    settings.set("custom_agents", stored)
    return existed


def filter_tools(tools: list[Any], agent: dict[str, Any] | None) -> list[Any]:
    if not agent or not agent.get("tools"):
        return tools
    allowed = set(agent["tools"]) | {"skill", "skills", "plan"}
    keep = []
    for t in tools:
        if t.server in allowed:
            keep.append(t)
        elif t.server not in BUILTIN_SERVERS and ("mcp" in allowed or t.server in allowed):
            keep.append(t)
    return keep or tools


def system_block(agent: dict[str, Any]) -> str:
    return (f"# You are running as the agent: {agent['name']}\n"
            f"{agent.get('description', '')}\n\n{agent.get('instructions', '')}").strip()


def tools() -> list[Any]:
    from .tools.base import READ, WRITE, Tool

    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        result = save(str(args.get("name") or ""),
                      instructions=str(args.get("instructions") or ""),
                      description=str(args.get("description") or ""),
                      skills=[str(s) for s in (args.get("skills") or [])],
                      tools=[str(s) for s in (args.get("tools") or [])],
                      boost=str(args.get("boost") or "auto"))
        if not result.get("ok"):
            return {"text": result["error"], "error": True}
        return {"text": f"Agent '{result['agent']['name']}' created (key {result['key']}). "
                        f"Pick it in the chat bar's agent menu, use it as a "
                        f"subagent role, or name it in a scheduled task."}

    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        items = all_agents()
        if not items:
            return {"text": "No custom agents yet."}
        return {"text": "\n".join(f"- {k}: {v['name']} — {v.get('description', '')}"
                                  for k, v in items.items())}

    return [
        Tool(name="agent__create", raw_name="create",
             description=("Create a reusable agent: name, description, standing "
                          "instructions, skills to load, tool families it may use "
                          f"({', '.join(TOOL_FAMILIES)}), boost auto/always/off. "
                          "Asks the user first."),
             input_schema={"type": "object", "properties": {
                 "name": {"type": "string"}, "description": {"type": "string"},
                 "instructions": {"type": "string"},
                 "skills": {"type": "array", "items": {"type": "string"}},
                 "tools": {"type": "array", "items": {"type": "string"}},
                 "boost": {"type": "string", "enum": ["auto", "always", "off"]}},
                 "required": ["name", "instructions"]},
             server="agent", origin="builtin", risk=WRITE, handler=_create),
        Tool(name="agent__list", raw_name="list",
             description="List the user's saved agents.",
             input_schema={"type": "object", "properties": {}},
             server="agent", origin="builtin", risk=READ, handler=_list),
    ]


def summary() -> dict[str, Any]:
    return {"agents": [{"key": k, **v} for k, v in all_agents().items()],
            "presets": PRESETS, "tool_families": TOOL_FAMILIES}
