---
name: building-and-projects
description: >
  This skill should be used when the user is building something — agents, skills, plugins,
  scripts, automation, apps, local LLMs, or when they ask "how do I build", "can you make an agent
  that", "improve my script", "which model should I use", "should this run local or cloud", or
  mention Codex, Ollama, LM Studio, n8n, MCP, Python or PowerShell. Load my-profile first.
metadata:
  version: "1.0.0"
---

# Building and projects

Check `me__about('tech-setup')` for the user's hardware and tools, and
`me__about('projects-and-builds')` for what they already have.

## Where work should run

| Task | Route |
|---|---|
| Quick private thinking, drafts | Local model |
| Anything with private or customer data | Local first — ask before sending to a cloud model |
| Real code changes, tests, repo work | The Coder agent or a coding CLI |
| Current facts, prices, docs | Web research with sources and dates |
| High-quality writing, long planning | The strongest cloud model available |
| Image generation | A hosted generator — not a CPU-only laptop |

## Hardware reality

Without a dedicated GPU, small quantised models (roughly 3-8B) are the practical ceiling for
local agent work. Don't propose infrastructure the user's machine can't run — say what it
would take instead.

## Design principles that hold up

1. **Evidence first.** Run the deterministic check, show its output, then interpret.
2. **Fail closed.** Unverified means "unknown" plus a next step, never a plausible guess.
3. **Separate detection from changes.** Read-only scans; changes behind explicit approval.
   Archive, don't delete.
4. **Redact secrets** before anything leaves the machine: tokens, passwords, cookies, keys.
5. **Name the maturity level:** idea → works once → tested → production-ready. Mock tests
   passing is not production-ready.
6. **Keep a holdout.** A score on data you tuned against is not a measurement.
7. **Log each iteration:** what changed, what was run, the result.

## Teaching style

Plain language, one copy-paste command at a time with its expected result, and wait for the
user's output before the next step when they are learning.
