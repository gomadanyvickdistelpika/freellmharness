"""Named agents, and spawning subagents for a piece of work.

An agent is a role: a system prompt, usually built from one of your skills, plus
whatever tools the harness has. Spawning one gives that role its **own context
window**, which is the point - a long VMware log triage does not have to share
a context with the job-search conversation that asked for it.

Rules, deliberately narrow:

* **One level.** A subagent cannot spawn another. Recursive agents are very hard
  to reason about and very easy to run away.
* **Same approval policy.** A subagent gets the same tools under the same gate.
  Spawning is not a way around an approval prompt - if the subagent calls a
  write tool, you are still asked.
* **Its own budget.** A smaller step cap than the parent, so a confused
  subagent stops early rather than consuming the whole run.
* **Returns text.** The parent gets the subagent's final answer as a tool
  result, with its tool calls surfaced in the parent's trace so you can see what
  it actually did.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from . import skills
from .config import settings

SUBAGENT_MAX_STEPS = 8
MAX_PARALLEL = 4                # v2: subagents that may run at once
SUBAGENT_TIMEOUT = 900.0        # fifteen minutes, then it is cut off

# Skills that describe *how to behave for the user* rather than a domain role.
# Always prepended, so a subagent inherits the ground rules.
BASE_SKILL = "my-profile"


@dataclass
class AgentRole:
    name: str
    description: str
    prompt: str
    source: str            # "skill" | "custom"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description[:300],
                "source": self.source, "prompt_bytes": len(self.prompt)}


def roles() -> dict[str, AgentRole]:
    """Every skill is available as a role a subagent can be spawned into."""
    out: dict[str, AgentRole] = {}
    for skill in skills.loadable():
        out[skill.name] = AgentRole(
            name=skill.name,
            description=skill.description,
            prompt=skill.body,
            source="skill",
        )
    # v2.1: your own agents are roles too - their instructions plus their skills.
    from . import custom_agents
    for key, agent in custom_agents.all_agents().items():
        bodies = [custom_agents.system_block({**agent, "key": key})]
        for name in agent.get("skills") or []:
            skill = skills.get(name)
            if skill:
                bodies.append(skill.body)
        out[f"agent:{key}"] = AgentRole(
            name=f"agent:{key}", description=agent.get("description", ""),
            prompt="\n\n---\n\n".join(bodies), source="custom")
    return out


def build_prompt(role: str = "", instructions: str = "") -> tuple[str, str]:
    """Returns (system_prompt, resolved_role_name)."""
    available = roles()
    blocks: list[str] = []

    base = available.get(BASE_SKILL)
    if base and role != BASE_SKILL:
        blocks.append(base.prompt)

    resolved = ""
    if role:
        chosen = available.get(role.strip().lower()) or \
            available.get(f"agent:{role.strip().lower()}")
        if chosen:
            blocks.append(chosen.prompt)
            resolved = chosen.name
    if instructions.strip():
        blocks.append(instructions.strip())

    if not blocks:
        blocks.append("You are a focused assistant. Do the task you are given, "
                      "then report back concisely.")

    blocks.append(
        "You are running as a subagent. You have your own context: the parent "
        "agent cannot see your working, only the answer you return. So finish "
        "with a self-contained answer - state what you found or did, what you "
        "could not do, and anything the parent needs in order to continue. "
        "You cannot spawn further subagents.")

    return "\n\n---\n\n".join(blocks), resolved


async def run_subagent(task: str, *, role: str = "", instructions: str = "",
                       provider=None, model: str = "",
                       max_steps: int = SUBAGENT_MAX_STEPS,
                       on_event=None) -> dict[str, Any]:
    """Run one subagent to completion and return its answer."""
    from . import agent as agent_mod
    from . import tools as tools_mod

    if provider is None:
        return {"ok": False, "text": "No provider available for the subagent."}

    system, resolved = build_prompt(role, instructions)
    available = [t for t in tools_mod.catalogue()
                 if not t.name.startswith("agent__")]           # no nesting

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task}]

    chunks: list[str] = []
    calls: list[str] = []
    stop_reason = "stop"
    error = ""

    async def drive() -> None:
        nonlocal stop_reason, error
        async for event in agent_mod.run(provider, model, messages,
                                         tools=available, max_steps=max_steps):
            etype = event.get("type")
            if etype == "delta":
                chunks.append(event.get("text", ""))
            elif etype == "tool_call":
                calls.append(event.get("name", ""))
                if on_event:
                    on_event({"type": "notice",
                              "text": f"  ↳ {resolved or 'subagent'}: "
                                      f"{event.get('name')}\n"})
            elif etype == "approval_request" and on_event:
                on_event(event)                     # the user must still answer
            elif etype == "error":
                error = event.get("text", "")
            elif etype == "done":
                stop_reason = event.get("stop_reason", "stop")

    try:
        await asyncio.wait_for(drive(), SUBAGENT_TIMEOUT)
    except asyncio.TimeoutError:
        stop_reason = "timeout"
        error = error or "The subagent ran past its time limit and was stopped."
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        stop_reason = "error"

    answer = "".join(chunks).strip()
    return {
        "ok": not error,
        "text": answer or "(the subagent returned nothing)",
        "role": resolved,
        "tool_calls": calls,
        "stop_reason": stop_reason,
        "error": error,
    }


# ---------------------------------------------------------------------------
# The spawn tool
# ---------------------------------------------------------------------------

def tools(provider=None, model: str = "", on_event=None) -> list[Any]:
    """The spawn tool, bound to the provider the parent run is using."""
    from .tools.base import WRITE, Tool

    if not bool(settings.get("subagents_enabled", True)):
        return []

    available = roles()
    role_list = ", ".join(sorted(available)) or "(none)"

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            return {"text": "A task is required.", "error": True}
        result = await run_subagent(
            task,
            role=str(args.get("role") or ""),
            instructions=str(args.get("instructions") or ""),
            provider=provider, model=model,
            max_steps=int(args.get("max_steps") or SUBAGENT_MAX_STEPS),
            on_event=on_event)

        header = (f"[subagent{' · ' + result['role'] if result['role'] else ''}"
                  f" · {len(result['tool_calls'])} tool calls"
                  f" · {result['stop_reason']}]")
        body = result["text"]
        if result["error"]:
            body = f"{body}\n\nError: {result['error']}"
        return {"text": f"{header}\n{body}", "error": not result["ok"]}

    async def parallel(args: dict[str, Any]) -> dict[str, Any]:
        jobs = [j for j in (args.get("tasks") or []) if isinstance(j, dict)
                and str(j.get("task") or "").strip()][:MAX_PARALLEL]
        if not jobs:
            return {"text": "Give a list of tasks, each with a 'task'.",
                    "error": True}
        results = await asyncio.gather(*(
            run_subagent(str(j["task"]), role=str(j.get("role") or ""),
                         instructions=str(j.get("instructions") or ""),
                         provider=provider, model=model,
                         max_steps=SUBAGENT_MAX_STEPS, on_event=on_event)
            for j in jobs), return_exceptions=True)
        parts, failures = [], 0
        for i, (job, result) in enumerate(zip(jobs, results), 1):
            if isinstance(result, Exception):
                failures += 1
                parts.append(f"## {i}. {job['task'][:80]}\nFailed: {result}")
                continue
            if not result["ok"]:
                failures += 1
            parts.append(f"## {i}. {job['task'][:80]}"
                         f" [{result['role'] or 'general'} · "
                         f"{len(result['tool_calls'])} tool calls]\n"
                         f"{result['text']}"
                         + (f"\nError: {result['error']}" if result['error'] else ""))
        return {"text": "\n\n".join(parts), "error": failures == len(jobs)}

    return [Tool(
        name="agent__spawn",
        raw_name="spawn",
        description=(
            "Run a focused subagent with its own fresh context, then get its "
            "answer back. Use it for a self-contained piece of work that would "
            "otherwise fill this conversation - triaging a long log, researching "
            "a company, drafting a document. Pick a role to give it the right "
            f"expertise. Available roles: {role_list}."),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "What the subagent should do. Be "
                                        "specific and self-contained - it cannot "
                                        "see this conversation."},
                "role": {"type": "string",
                         "description": f"One of: {role_list}"},
                "instructions": {"type": "string",
                                 "description": "Extra system instructions, "
                                                "optional"},
                "max_steps": {"type": "integer",
                              "description": f"Default {SUBAGENT_MAX_STEPS}"},
            },
            "required": ["task"],
        },
        server="agent",
        origin="builtin",
        risk=WRITE,          # it can call write tools, so it asks like one
        handler=handler,
    ), Tool(
        name="agent__spawn_parallel",
        raw_name="spawn_parallel",
        description=(
            f"Run up to {MAX_PARALLEL} independent subagents at the same time "
            "and get all their answers back - e.g. research three companies, "
            "or draft a CV and a cover letter in parallel. Each task must be "
            "self-contained."),
        input_schema={
            "type": "object",
            "properties": {"tasks": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "task": {"type": "string"},
                    "role": {"type": "string",
                             "description": f"One of: {role_list}"},
                    "instructions": {"type": "string"}},
                    "required": ["task"]}}},
            "required": ["tasks"],
        },
        server="agent", origin="builtin", risk=WRITE, handler=parallel,
    )]


def summary() -> dict[str, Any]:
    return {"enabled": bool(settings.get("subagents_enabled", True)),
            "roles": [r.to_dict() for r in sorted(roles().values(),
                                                  key=lambda r: r.name)],
            "base_skill": BASE_SKILL,
            "max_steps": SUBAGENT_MAX_STEPS,
            "nesting": False}
