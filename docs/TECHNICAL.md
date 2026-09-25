# AEGIS — technical guide

*The friendly quick start is in the main [README](../README.md). This is the deep dive.*

A local AI harness for Windows — version 2, tuned for free models. One window that talks to free and local models, paid APIs
and your existing CLI subscriptions — with an LM Studio-style model browser that
tells you whether a model will actually run on this machine *before* you
download it, MCP servers imported from whatever you already have configured,
computer and browser use, your notes vault indexed for search, seventeen
preinstalled skills, and subagents that can be spawned into any of them.

Built for an ordinary laptop with 16 GB of RAM and no CUDA GPU, which means the
hardware honesty is not decoration — it is the point.

---

## What's new in AEGIS 2.1

Built to make AEGIS work like Claude and Codex on free models.

**Talk to it.** 🎤 in the message box: click to talk, click again to send it to
text. Your voice is transcribed by Groq's free Whisper (or Pollinations, or
OpenAI, or `faster-whisper` on this PC if you install it). If none of them
answers, the browser's own speech recognition takes over. Right-click 🎤 for
**hands-free mode**: you talk, it sends, the answer is read aloud (🔈/🔊 in the
chat bar), and it listens again.

**Slash commands.** Type `/` for the menu: `/image`, `/music`, `/video`,
`/voice`, `/search`, `/code`, `/agent job-hunter`, `/project …`,
`/browser <url>`, `/computer …`, `/schedule …`, `/boost on|off|auto`, `/talk`,
`/speak`, `/undo`, `/files`, `/new`, `/help`.

**Any file in, any file out — up to 30 GB.** Attach any file type with ＋. Each file streams straight to disk with a progress bar (nothing is held in memory), up to 30 GB by default (Settings → *Largest file you can attach*; AEGIS checks free disk space first). Files over 256 MB are stored and handed to the tools by path instead of being read into a prompt, and they are hard-linked into the workspace, so a 30 GB file does not take 60 GB of disk. It is read if it can
be read, and it is always saved to the workspace (`uploads/`) so the code, media
and voice tools can work on zips, audio, video and more. Files the agent
writes show a ⬇ download chip. 📁 lists everything in the workspace, ready to
download.

**A browser beside the chat.** 🌐 Browser docks the agent's Chromium next to
the conversation. It opens by itself when the agent starts browsing. You can
click on the live view, type into the page, scroll, or use the address bar.
**Take over** pauses the agent's clicks while you sign in, then **Hand back**.
Computer use is still there for desktop apps, and it always asks first.

**Several keys per provider, FreeLLMAPI-style.** Add extra free-account keys
for the same provider. When a key is rate-limited or spent, AEGIS rotates to the
next key before switching model, then fails over across providers as before.

**Every free model can use tools.** A model with no native tool calling gets a
small text protocol (`<tool_call>{…}</tool_call>`) that AEGIS turns into real
tool calls. So CLI subscriptions and text-only free models can search, edit files
and run code too.

**Boost.** For hard requests (code, analysis, CVs, VMware, research, and
anything your agent marks "always"), the model gets a plan-first instruction and
a visible checklist (`plan__update`). When it finishes, a **different free
model reviews the answer**. If the reviewer finds real problems, the answer is
rewritten, and the first draft stays folded above it. Settings or the chat bar:
auto / on / off.

**Codex-style coding.** Before you approve a file change, the approval card
shows the actual **coloured diff**, or the exact command for `code__run`. Every
write is **checkpointed**: `files__undo`, `/undo`, or the list on the Agents tab
reverts it. `code__git` runs read-only git without asking. `AGENTS.md` /
`CLAUDE.md` in a project folder are followed as repository rules.

**Your agents.** There are seven ready-made agents: Coder (Codex-style), Job
Hunter, VMware TSE, Researcher, Studio, Family Tutor and Daily Planner. Or make
your own: instructions, skills, allowed tool families, route and boost. Pick one
in the chat bar, run a scheduled task as one, or use it as a subagent role. The
assistant can also create agents when you ask it to in chat (it asks you first).

**MCP in one click.** The Agents tab has a catalogue: GitHub, Google Workspace
(Gmail/Drive/Calendar), filesystem, fetch, git, memory graph, sequential
thinking, time, Playwright, Brave Search, SQLite. Fill in the token or folder
and press Install. The new `mcp-builder` skill lets the agent write its own
Python MCP server and register it.

**What it knows about you, and who sees it.** The **Me** tab holds editable notes
you fill in yourself (they start as `{{template}}` placeholders): about you, working
style, career, job-search rules, projects, setup, interests, family, health and money. Each note
is **public** (in every prompt), **personal** (looked up when a task needs it)
or **private**. Private notes, and the private life-OS skills, go to local
models freely. A cloud model has to ask first (`me__private`), and you approve or
decline. Your edits are never overwritten.

---

## What's new in AEGIS 2

AEGIS 2 is built for one goal: **Claude-style help on free models, without
errors when a free allowance runs out.** Everything from v1 is still here; this
is what changed.

### Free models in one step
**Providers → Quick setup.** Paste any keys you have — OpenRouter, xKiro,
9Router, Pollinations, Google AI Studio (Gemini), Groq, Cerebras, Hugging Face,
GitHub Models, NVIDIA NIM, FreeLLMAPI. AEGIS saves each provider, asks what is
free *today*, builds the smart **Auto** route and makes it your default. It
refreshes that route daily until you edit it by hand. One key is enough.

9Router's preset now points at its local default (`http://localhost:20128/v1`):
run `npm install -g 9router`, then `9router`, connect Kiro and friends in its
dashboard and paste the key it shows.

### Never an error when a model runs dry
v1 already switched models on 402/429/5xx. v2 also moves on — silently where it
can, with a one-line note where it matters — when:

| What happened | v2 |
|---|---|
| The model can't use tools (`400 … does not support tools`, OpenRouter's `404 No endpoints found that support tool use`) | Next model. The gap is **remembered**, so next time that model is skipped for tool requests but still used for plain chat |
| The model can't see images | Next model, remembered |
| The conversation is longer than its context | Next model, its limit remembered |
| One provider rejects its key (401) | That provider rests 15 min; the route carries on with the others |
| The model answers with nothing | Next model |
| An error arrives *inside* a 200 stream (OpenRouter does this) | Classified like any other failure |
| `free-models-per-day` style messages | Treated as a daily quota: rested until midnight |
| No tool-capable model is free right now | Answers without tools instead of failing |

Only a genuinely malformed request still stops — every model would fail it the
same way. Free and paid still never mix, **unless you choose** an overflow:
each route has *"When spent, continue with…"* — point your free route at a paid
one and AEGIS switches when the free models are exhausted, and says so in the
chat ("this route may cost money"). Off by default.

### Smart routing
With **Smart** on (the default for Auto), each message is read before a model is
chosen: code goes to coder models, images to models that can see, long
documents to big-context models, reasoning to thinking models, a quick question
to the fastest provider (Groq/Cerebras) or the model with the best measured
tokens/sec. A chip above each reply shows the pick, e.g.
`code (looks like code) → openrouter/qwen/qwen3-coder:free`. Model abilities
come from each provider's catalogue (tools, vision, context size) and from what
models refuse at run time.

### Super Agent
Every chat now goes through an orchestrator:

* a **core prompt** that makes free models behave like a careful assistant —
  today's date, Markdown formatting, plan → act → verify, search before stating
  current facts, the money/medical/legal boundaries;
* a **weighted intent classifier** over 16 specialists (your 12 life-OS skills
  plus new **coding**, **research**, **media-studio** and **planner**) that
  *preloads* the one or two that apply — so weak models and models without tools
  still get the right expertise;
* a **tool diet**: only the tool families a request needs are offered (MCP
  servers always are), which keeps small models from drowning in 40+ schemas;
* **parallel subagents** — `agent__spawn_parallel` runs up to four at once.

**Privacy:** free providers may log prompts. By default only a short,
non-sensitive profile goes out with every request; the full `my-profile`
(household, health) is loaded on demand, and local models always get it.
Settings → *Your profile in prompts*.

### Coding like Claude Code
New tools, all fenced to the project workspace, connected folders or the
general AEGIS workspace:

* `code__run` — PowerShell commands (build, test, pip/npm, git), with exit code
  and output; **always asks first**; a short list of disk-destroying commands is
  refused even if approved.
* `code__python` — run a Python snippet.
* `files__edit` — exact-snippet replace with a diff, the safe way to change code.
* `web__search` / `web__fetch` — search (DuckDuckGo with no key; Brave, Tavily or
  your SearXNG if configured) and read pages/PDFs. Loopback and LAN addresses are
  refused, so a web page can't steer the agent into AEGIS's own API.

Replies now render Markdown: headings, lists, tables, and code blocks with a
Copy button; `<think>` blocks from reasoning models fold away.

### Studio — images, voice, music, video
A new tab, and `media__generate_*` tools the agent calls when you ask in chat
("make a thumbnail for…"). Each kind has an ordered list of backends with the
same failover and free/paid rules as chat routes:

| Kind | Default order |
|---|---|
| Image | Pollinations (your key) → OpenRouter image model → Pollinations keyless |
| Voice | Pollinations speech → **Windows voices, offline** |
| Music | a Hugging Face Space or gateway music model if you set one up → **Suno pack** |
| Video | Pollinations video → **slideshow**: generated stills + slow zoom + optional narration, stitched by ffmpeg, offline |

Being straight about music: there is no reliable free music-generation API
today. So the always-works fallback writes a paste-ready **Suno pack** (title,
style prompt, tagged lyrics) with your chat route — the workflow you already
use. If a gateway you have offers a music model, add it as an `http_post`
backend; a Hugging Face Space (e.g. ACE-Step) works through `gradio` after
`pip install gradio_client`. Everything is saved under
`%LOCALAPPDATA%\Aegis\media`, shown in chat and the gallery, and copied into
the project workspace when you are in a project.

### Projects
Claude-style Projects, on your disk. A project has a name, a goal,
instructions for every chat, an optional route and pinned specialists, and a
workspace folder (AEGIS's own, or any folder such as a git repo) holding:

* `PROJECT.md` — living notes read into every chat; the agent updates
  *Decisions* and *Next steps* as they change;
* `knowledge/` — reference files listed in every chat and read on demand;
* `media/`, `task-output/` — what the Studio and scheduled tasks produce.

Pick a project in the chat bar, or just say "make this a project". The agent can
also **schedule tasks** from chat (`project__schedule`: "every weekday at 8,
find five new remote roles") — they run inside the project and save a dated
file there. Register them with Windows on the Files & Tasks tab to run while
AEGIS is closed.

### Upgrading from v1
AEGIS 2 lives in its own folder and uses the same data folder
(`%LOCALAPPDATA%\Aegis`), so your providers, keys, routes, chats, skills and
memory carry over. The Auto route is added once. Close v1 before starting v2 —
they share a port. First start builds a fresh virtual environment (a few
minutes).

---

## Run it

Double-click **`run.bat`**.

First run creates a virtual environment and installs dependencies (about two
minutes). Every run after that opens in a couple of seconds. You need Python
3.10 or newer from python.org with "Add to PATH" ticked.

The window is Edge WebView2, which is already on Windows 10 and 11. If it is
missing, AEGIS opens in your default browser instead rather than refusing to
start.

| Command | What it does |
|---|---|
| `run.bat` | Normal desktop window |
| `run.bat --browser` | Use your default browser instead |
| `run.bat --no-window` | API only, no UI — for scripting |
| `run.bat --port 9000` | Different port |

Everything AEGIS writes lives in `%LOCALAPPDATA%\Aegis` — models, settings,
encrypted secrets, logs. Delete that folder and AEGIS is gone without trace.

---

## The tabs

### Chat
Pick a provider and a model, type. The reply streams in. Every backend looks the
same here, which is the whole idea — a local 3B and Claude Code go through one
window.

The **Tools** switch in the chat bar decides whether the model can call MCP and
computer-use tools. When it does, each call appears inline in the transcript —
name, arguments, result, screenshots — so you can see what happened rather than
trusting a summary.

Chats save themselves. The sidebar lists everything, searchable across every
message. Full section below.

### Models
Three sources merged into one list:

- **Installed** — everything Ollama has pulled, everything LM Studio can serve,
  and every GGUF that AEGIS downloaded itself
- **Pullable** — a curated shortlist you can pull into Ollama with one click
- **Hugging Face** — search any GGUF repo on the Hub, expand it to see each
  quantisation, download the one you want

Every card carries a verdict. See below.

### Browser
AEGIS's own Chromium, with its own logins, separate from your Chrome. Address
bar, live view, and the same numbered elements the agent sees — so you and it
share one session and can hand a page back and forth. Full section below.

### Tools &amp; MCP
Approval policy, computer use, and every MCP server — connected, discovered on
this machine, or added by hand. Full section below.

### Files &amp; Tasks
Connected folders, attachment stats, and scheduled tasks with run-now and
history.

### Skills &amp; Memory
The preinstalled skills, the agent roles they provide, your vault index,
and everything the agent has committed to memory. Also where self-written skills
wait for your approval.

### Providers
Where tokens come from, and whether each route is actually working right now.
API keys go in here. So does browser sign-in.

### Settings
The knobs that change every verdict — context length, RAM headroom, assumed
memory bandwidth — plus what this machine actually is, and the llama.cpp runner.

---

## The can-it-run gauge

This is the part you asked for, and it is arithmetic rather than a guess:

```
required = weights + KV cache(context) + compute buffer + overhead
```

The weights figure is the real file size. The KV cache is computed from the
model's own GGUF header:

```
2 (K and V) × layers × context × kv_heads × head_dim × 2 bytes
```

AEGIS reads that header **without downloading the model** — a few HTTP range
requests pull the first few KB from Hugging Face, which is how it can give a
real verdict on a 40 GB file you have not touched. If the header will not parse,
it says `KV source: estimated` and falls back to a size ratio rather than
pretending to know.

| Badge | Meaning |
|---|---|
| 🟢 **Full GPU offload** | Fits in VRAM with headroom |
| 🟢 **Runs on CPU** | Fits in free system RAM with headroom |
| 🟡 **Partial offload** | Some layers on GPU, some on CPU. Works, slower |
| 🟡 **Tight — close some apps** | Fits your total RAM but not what is free now |
| 🔴 **Will not fit** | It will thrash swap. Do not bother |

Each card shows its own workings — weights, KV cache, overhead, total against
budget — so when a verdict looks wrong you can see which number moved.

**The context slider matters more than people expect.** On a 16 GB machine, an
8B Q4 model at 4k context needs about 5.2 GB and runs fine. The same model at
128k context needs 21 GB, because the KV cache alone is 16 GB. AEGIS shows that
flip; most tools do not.

There is also a throughput ceiling in tokens/sec. That is a physics bound —
memory bandwidth divided by model size — not a promise. Real throughput lands at
roughly half of it. It is labelled as an estimate everywhere it appears.

---

## Skills, agents and memory

### The preinstalled pack

Seventeen skills ship in the box: `my-profile`, `coding`, `research`, `planner`,
`media-studio`, `mcp-builder`, `vmware-support`, `job-search`,
`family-and-school`, `money-and-benefits`, `immigration`,
`health-and-medical`, `home-car-and-diy`, `creative-music`,
`fitness`, `forms-and-admin`, `building-and-projects`. See
`aegis/skills_bundled/PACK.md` for how to change or add your own.

**Only the descriptions go into the prompt.** Seventeen full skills would swamp the
context, so the agent gets a one-line index and calls `skill__load` to pull in
the body when one applies — the same routing you already do by hand, made
explicit. Your own skills go in `%LOCALAPPDATA%\Aegis\skills`, and a skill there
overrides a bundled one of the same name.

These files contain your real personal and family details. They are on your
machine and they go into prompts sent to whichever provider you pick — worth
knowing before you route a family question to a cloud model.

### Self-written skills, behind a gate

The agent can write a skill for a task it expects to repeat. It lands in
`skills_pending` and **does nothing at all** until you read it on the Skills tab
and click approve — it is not loaded, not indexed, not offered.

The gate is structural, not a promise: the agent has `skill__write`, and there
is no approve or reject tool anywhere in its catalogue. It cannot approve its own
work because there is no call that would do it. Tested by drafting a skill and
asserting it stays out of the index, out of `skills.get()`, and that the tool set
contains `write` but not `approve`.

A skill is a standing instruction that shapes every future reply. That is why it
gets a human in the loop and a tool call does not.

### Subagents

Every skill doubles as a role. `agent__spawn` runs a subagent with its own system
prompt and **its own fresh context** — a long VMware log triage no longer has to
share a context window with the job-search conversation that asked for it.

Four deliberate limits:

- **One level.** A subagent cannot spawn another. This is structural too: spawn
  is bound to the run's provider and added per run, so the plain catalogue a
  subagent receives has no spawn tool in it.
- **Same approval policy.** Spawning is not a way around an approval prompt.
- **Its own budget** — 8 steps, 15 minutes, then it stops.
- **Returns text**, with its tool calls surfaced in the parent's trace.

### Your notes vault

Point AEGIS at your Obsidian folder and it builds a full-text index — `.md`,
`.markdown` and `.txt`, skipping `.obsidian`, `.git` and anything over 1 MB. The
agent gets `vault_search` and `vault_read`.

**Nothing writes to your vault.** Not a disabled function, not one behind a flag
— there is no write call in `memory.py` at all, and the test suite greps the
module for write primitives to keep it that way.

Separately, the agent keeps its own memory: durable facts it saves across
conversations, searchable, and fully editable and deletable by you on the Skills
&amp; Memory tab. It is your memory, not a black box.

---

## Browser use

AEGIS browses in **its own Chromium**, always, unless you say otherwise. It is
launched by Playwright with a persistent profile inside the AEGIS folder, it
opens as a real window you can take the mouse to, and it is mirrored into the
**Browser tab** so you can watch and drive it without leaving the harness.

This is a deliberate default, and the reasoning matters:

- Your Chrome is *yours*. It holds your mail, your bank, your Broadcom sessions
  and the tabs you are in the middle of. An agent loose in it can close
  something you needed or act inside a session you never meant to lend it.
- AEGIS's own Chromium starts signed out of everything. You sign in to what a
  task actually needs, once, and it sticks in that profile. The blast radius is
  one folder you can delete.

**There is no automatic fallback, in either direction.** Ask for Chrome and find
it unreachable and you get an error saying so — not a quiet substitution. A
silent swap either way is how an agent ends up somewhere you did not send it.

### The Browser tab

An address bar, back/forward/reload, a live view of the page, the page text, and
the same numbered elements the agent sees — each with a button, so clicking [4]
in the harness does exactly what the agent clicking [4] would do.

You and the agent share **one session**, deliberately. So a task that hits a
sign-in can be handed over mid-flight: you sign in on the Browser tab, the agent
carries on from the page you left it on. Two separate browsers would mean doing
that dance twice.

The live view polls about once a second rather than streaming, and stops when
you leave the tab. A page being read does not change, so a frame a second is
plenty and costs nothing when you are not looking.

### The three ways to browse, and when each applies

| | What it is | When |
|---|---|---|
| **AEGIS Chromium** | Page-level control of its own browser | Everything, by default |
| **Your Chrome** | Attached over CDP, inherits your sessions | Only when you ask for it by name |
| **Computer use** | The real mouse and keyboard on your desktop | Apps that are not web pages, or when you ask |

The agent is told this in the tool descriptions themselves, not just here, so it
picks correctly without being reminded each time. Switching into your Chrome is
its own tool, classified as a write, and refuses unless it is confirmed — and
its description says plainly never to use it because a page wanted a login or
because something else failed. Those are reasons to sign in to AEGIS's browser,
not reasons to go rummaging in yours.

To use your Chrome: change **Which browser agents use** on the Tools tab, or
just ask in chat. Either way Chrome must have been started with
`--remote-debugging-port=9222`, or nothing can attach to it.

**Elements are addressed by reference number, not CSS selector.** Reading a page
tags every link, button and field and returns a numbered list; clicking takes
that number. Models are poor at inventing selectors and good at picking from a
list, and a stale reference fails loudly instead of clicking the wrong thing.

Navigation is classified as a read. That is a judgement call — asking permission
for every URL would make browsing unusable, and it does mean a link whose GET has
a side effect could be followed without a prompt. Clicking and typing are writes
and always follow your policy.

---

## Files: attachments and folders

### Attaching files

The **＋** button in the composer, or drag files onto it. Every attachment is
extracted to text once, on arrival — `.pdf`, `.docx`, `.xlsx`, `.csv`, `.json`,
source code, anything that decodes as text. Images go to the model as images.

Then the split that matters:

- **Under 12,000 characters** — the text goes straight into the message.
- **Over that** — it is stored, indexed, and the model is told its id and how to
  search it. It pulls only the parts it needs.

A 300-page PDF is roughly 400,000 tokens. Pasting one in blows past most context
windows and would spend a whole day's free allowance on a single message. So it
costs a search instead.

Extraction is honest about failing: a scanned PDF with no text layer says
*"3 pages, but no text layer — this looks like a scan"* rather than attaching
nothing and letting you wonder.

### Connecting folders

Connect a folder on the **Files & Tasks** tab and the agent gets `files__list`,
`files__read`, `files__search` and `files__write` inside it. Reading is free;
writing follows your approval policy.

**The fence is real.** Every path is resolved to its actual location on disk —
following symlinks, flattening `..` — and checked against the connected roots
before anything opens it. Connect nothing and the agent has no filesystem at all.

Two things the tests caught that are worth knowing about:

- **Backslashes are treated as separators on every platform.** On Linux `\` is a
  legal filename character, so `..\secret.txt` would be one oddly-named file
  *inside* the fence there and a genuine traversal on Windows. AEGIS takes the
  stricter reading everywhere.
- **UTF-16 is only tried when a BOM says so.** Decoding blind is a trap: any
  even-length byte string "succeeds" into nonsense, so `café` in cp1252 came back
  as `慣` instead of falling through to the encoding that was right.

---

## Scheduled tasks

A prompt that runs on a schedule — a daily job search, a weekly summary. Each run
gets **its own chat** you can open and continue, plus a **dated Markdown file**
in a folder you choose.

### It runs when AEGIS is closed

Two layers, because either alone is wrong. An in-app loop fires due tasks while
AEGIS is open. And a real **Windows Task Scheduler** entry wakes AEGIS headless
(`python -m aegis --run-task <key>`), runs one task and exits — so an 8am job
search happens whether or not you have opened the laptop.

Cron shapes that name a fixed time translate to Task Scheduler cleanly; an
`every 15 minutes` schedule is refused for Windows with an explanation, because
Task Scheduler wants a time of day.

### Missed runs

If yesterday's 8am run never happened because the machine was off, it runs **once**
when you next open AEGIS. Once — not once per missed day. Coming back from a week
away should not produce seven identical job searches, and that is tested.

### Unattended approvals

A scheduled run has nobody watching, so anything needing approval is **declined
immediately** rather than stalling five minutes on a prompt no one will see. The
model is told it was declined and carries on.

A task can be marked **trusted**, which auto-approves instead. It is off by
default, and worth turning on only for a task you have watched work with
**Run now** first.

---

## Routes: many models, one conversation

A route is an ordered list of models. AEGIS uses the highest one that is
healthy, and drops down the list when one runs out of tokens, gets rate-limited
or starts failing. You pick a route in the chat bar instead of a single model.

### What makes it switch, and what does not

| Failure | What happens |
|---|---|
| 402 out of credit, free allowance spent | Switch, long cooldown |
| 429 rate limited | Switch, short cooldown, honours `Retry-After` |
| 5xx, timeout, connection dropped | Switch, short cooldown |
| 404 model no longer exists | Switch, rest it for a day |
| 400 malformed request | **Stop and tell you** |
| 401 bad API key | **Stop and tell you** |

That distinction is the whole point. Walking through five models on a request
none of them can answer wastes time and hides the real problem, so a bad request
surfaces immediately.

One subtlety worth knowing: a `429` whose body says *"you have used your free
allowance for today"* is classified as **quota**, not rate limit — it gets a
30-minute rest rather than a 60-second one, because retrying in a minute is
pointless.

### When a model dies mid-sentence

The text already written is **kept**, the break is marked in the trace, and the
next model is asked to carry on from exactly where it stopped. You get a
complete answer with an honest seam rather than a lost reply or a bill for
generating it twice.

A half-built *tool call* is the exception — partial arguments are worthless, so
that turn restarts cleanly on the next model.

### Free and paid never mix

A route marked free-only **cannot contain a paid model at all**. That is enforced
server-side, not just in the UI: try to add one and it is stripped on save. If
every free model is exhausted, the route stops and says so.

This is deliberate. The alternative — falling back to Claude when the free tier
runs dry — means a long test session quietly spending real money. AEGIS would
rather stop.

Local models (Ollama, LM Studio, llama.cpp) always count as free. A model id
ending `:free` counts as free. A provider you have ticked **My whole plan here
is free** counts as free, all of it. Everything else is assumed to cost, because
being wrong the other way spends your money.

### Whole-account free plans

xKiro, 9Router, FreeLLMAPI and an Atria preview key all work the same way: the
allowance belongs to your *account*, not to any one model, and their catalogues
carry no prices to prove it. Tagging forty models by hand to use a free tier is
not a workflow, so there is a checkbox on the provider — **My whole plan here is
free** — and every model on it becomes usable on a free-only route.

That is you asserting something about your own account. AEGIS does not guess it,
and does not ship a table of who is free this month, because that table would be
wrong by the time you read it.

### Build my free route

One button on the Routes tab. AEGIS asks every provider you have added what it
currently offers, takes the free ones, and writes them all into the `test` route
in this order:

1. **Free hosted models**, because they are far faster than anything a CPU runs.
2. **Local engines last**, because a model on your own machine is the one thing
   that can never run out.

So when xKiro's daily allowance is gone the route falls to OpenRouter, then
9Router, then whatever else you have, and only lands on Ollama when every hosted
option is spent. No prompt, no error, no model picker. Reorder or trim it
afterwards like any other route.

### FreeLLMAPI

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) is a separate
open-source gateway you run yourself. You paste your free keys for Google, Groq,
Cerebras, Mistral, Cohere, NVIDIA and the rest into it once, and it hands out a
single OpenAI-compatible endpoint on `http://localhost:3001/v1` with one token
that fronts all of them.

It is in the preset list, pre-ticked as a free plan. Start it before AEGIS; if
nothing is listening on port 3001 the provider simply reports as unreachable.

Its rotation and AEGIS's routing overlap, and that is fine — they nest. FreeLLMAPI
rotates *within* its own pool of keys; AEGIS treats the whole gateway as one
candidate and falls through to your other providers when the entire thing is
spent. You do not have to choose: adding it is one more free source on the same
route.

### Health and speed

Every model carries a record: successes, failures, what it last failed on, how
long it is resting, and its measured tokens/sec (an EWMA from real runs, not a
published figure). It persists, so a model that hit its daily cap is still
resting after a restart. **Try them all again now** on the Routes tab lifts
every cooldown without losing the speed measurements.

---

## Adding providers: OpenRouter, Groq, 9Router, anything

Any endpoint serving `/chat/completions` can be added with three fields — a
short name, a base URL and an API key. Presets fill the URL for OpenRouter,
**xKiro**, **Atria**, Groq, Together, DeepInfra, Cerebras, Mistral and a
self-hosted 9Router; all of them stay editable, and "Something else" takes a
blank URL for a LiteLLM gateway or a work proxy.

| Preset | Base URL | Notes |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | Publishes prices; `:free` models auto-detected |
| xKiro | `https://api.xkiro.com/v1` | `vendor/model` ids. Has `/models`, publishes no prices |
| Atria | `https://api.atria-asi.ai/v1` | `Atria-Dawn-Preview`, 256K ctx. **No `/models`**, 60 rpm |

**List models** in a provider's card reads its live catalogue, and every row has
a **free/paid toggle** and an **add to route** dropdown — so a model id like
`openai/gpt-5.6-sol` goes into a route with one click instead of being retyped.

**AEGIS ships no table of free tiers.** Free allowances and model names change
faster than any list could survive, so **List models** queries the provider's own
`/models` endpoint and marks each model free or paid from the price it reports
(or an OpenRouter `:free` suffix). What you see is what that provider says today.

Keys go in the encrypted vault, never into `settings.json`.

### Providers without a `/models` endpoint

Not all of them publish one — **Atria** documents only `/chat/completions`.
Probing for a model list and failing would report a perfectly good provider as
broken and leave it with no models for a route to use.

So a provider can be marked as having no `/models` endpoint, and you list its
model ids by hand. AEGIS then:

- reports it ready on the strength of the saved key, and says plainly that it
  could not verify against anything;
- serves the models you entered, capitalisation intact (Atria's
  `Atria-Dawn-Preview` is case-sensitive);
- **assumes those models cost money**, because it has no price to read. A
  free-only route will refuse them until you press **mark free** on the route.

That last step is deliberate friction. Guessing "probably free" on a provider
that publishes no pricing is exactly how a free route quietly starts billing.

### Catalogues that publish no prices

There are two different meanings of "free" in the wild, and they need different
handling:

- **A property of the model.** OpenRouter's `:free` models and anything priced
  at zero. AEGIS reads this from the catalogue and tags it automatically.
- **An allowance on your plan.** xKiro's free tier is *500,000 tokens a day
  across 40+ models* — nothing about the model id tells you that, and the same
  model costs money once the allowance is spent.

For the second kind AEGIS cannot know, so it assumes paid and you mark the ones
your plan covers. **Those hand-set tags survive a refresh** — only a provider
that actually publishes prices is allowed to overwrite them. Without that, a
free route would silently empty itself every time you pressed *List models*.

When a daily allowance does run out, the provider returns a 402 or 429 and the
router treats it exactly like any other quota failure: rest that model, move to
the next one. Which is the behaviour you wanted in the first place.

A 429 whose body mentions a *daily* limit is rested until just after midnight
rather than for the usual half hour. Retrying a spent daily allowance every
thirty minutes all afternoon is a dozen requests that cannot possibly succeed,
on a key that is already being rate-limited.

### Allowances: stepping off before the refusal

Everything above is what happens *after* a model refuses. The Allowances panel
is how you avoid the refusal in the first place.

Fill in what your plan gives you — requests per minute, requests per day, tokens
per day — and AEGIS counts down to it. When the count is spent the model is
stepped over on the next turn: no failed request, no 429, no error in your chat.
Use `*` as the model to cover a whole provider, which is usually right, because
a free plan's cap is on the account rather than on each model.

You do not have to fill anything in. Many providers report what is left on every
response, in headers like `x-ratelimit-remaining-requests` and
`x-ratelimit-reset-requests`, and AEGIS reads those whether or not you have set
a number. When one says *nothing left, back in 45 seconds*, that is not a guess
to be improved on — the model is stepped over for exactly 45 seconds.

Where a header implies a limit, AEGIS keeps it and marks it **from the
provider**. It has to infer whether the number is per-minute or per-day from the
reset interval, because `x-ratelimit-limit-requests` means a daily cap at one
provider and a per-minute one at another — the name alone settles nothing. Any
number you type in beats an inferred one, and a number on a specific model beats
a provider-wide default.

Counts are per (provider, model), persist across restarts — so closing AEGIS
does not hand you a fresh allowance you do not actually have — and reset at your
local midnight. **reset** on the panel clears today's count by hand if you top up
a plan mid-day.

### Retry-After

When a provider says *when* to come back — Atria sends `Retry-After` alongside
`x-rpm-limit` and `x-rpm-remaining` on a 429 — AEGIS uses that instead of its own
cooldown, in both directions: a 300-second hint overrides the 60-second default,
and a 5-second hint shortens a quota rest that would otherwise be 30 minutes.
Capped at six hours, so a malformed header cannot park a model forever.

---

## Chat persistence

Everything is saved to `chats.db` in the AEGIS folder, automatically. No save
button, no "do you want to keep this".

**The server owns the transcript.** The browser holds a chat id and sends your
new message; it never posts the history back. That one decision is what makes
the rest work — images stay attached to the tool calls that produced them, and
a run does not depend on a browser being connected.

### Runs survive the window closing

An agent run is a background task writing into an event buffer. HTTP connections
*subscribe* to that buffer rather than driving it. So:

- Close the window mid-run and it **keeps going**.
- Reopen and it **replays what you missed**, then continues live.
- **A pending approval is still waiting**, because the approval gate lives
  server-side and the loop is still blocked on it.

That last one is tested by starting a run with nobody watching at all, letting it
reach an approval, then attaching and answering it.

AEGIS reopens your most recent chat on launch, preferring one that is still
running.

### What a saved chat holds

Two representations of the same conversation, deliberately:

- **canonical** — what goes back to the model: roles, content, tool calls, tool
  results and the images tools returned. Source of truth for continuing.
- **display** — the exact part list the UI renders, including approval outcomes
  and step markers. Stored so a reloaded chat looks like it did live, rather
  than a reconstruction that quietly loses the approvals.

Images live in their own table as raw bytes, referenced by id. That keeps message
rows small and lets a screenshot load lazily when you scroll to it.

### The image budget

One computer-use session can produce fifty screenshots. Past **48 MB per chat**
(adjustable) the oldest images are evicted, the newest is never evicted, and both
the trace and the model are told the picture is gone rather than silently seeing
nothing.

### Search and export

Full-text search across every message via SQLite FTS5, falling back to `LIKE` if
your Python was built without it — the app says which it is using in the sidebar
footer. Export any chat as Markdown (with the tool trace as block quotes) or as
JSON.

Chats can be renamed, pinned, archived and deleted. A chat names itself from your
first message.

---

## Tools, MCP and computer use

### Importing what you already have

AEGIS reads the config files other MCP hosts write, so a server you set up once
shows up here without retyping it:

| Host | File |
|---|---|
| Codex | `~/.codex/config.toml` → `[mcp_servers.*]` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| VS Code | `%APPDATA%\Code\User\mcp.json` and `settings.json` |
| Cursor | `~/.cursor/mcp.json` |

So your Drive and Gmail servers in Codex appear on the Tools tab. Tick them,
import, connect. **Discovery never connects anything** — importing copies the
config, enabling is a separate, explicit click.

Environment variables come across so the server can actually authenticate, but
their **values are never sent to the browser** — the UI shows key names only.
That is enforced in `Discovered.to_dict()` and tested.

Both transports work: **stdio** (a local subprocess — how Codex, Claude Desktop
and VS Code run local servers) and **streamable HTTP** (hosted servers, with
session-id handling).

### The agent loop

Model → tool calls → run them → feed the results back → repeat, capped at 15
model turns. Four behaviours worth knowing:

- **A failed tool is not a failed run.** The error goes back to the model as a
  tool result so it can correct itself.
- **A declined call is also just a result.** The model is told you declined and
  continues without it, instead of the run dying.
- **The cap is real.** At 15 turns it stops and says so. Tested with a model that
  loops forever.
- **Tool output is data, never instructions.** A tool that returns "ignore your
  previous instructions" is a tool returning that string, and the system note
  says so explicitly.

Tool calling needs a model that supports it. qwen2.5, llama3.1 and mistral do;
most sub-3B models will ignore the tools and just talk. That is the model's
limit, not the harness's.

### Approvals

Default: **reads run automatically, writes ask.**

Classification uses the MCP `readOnlyHint` and `destructiveHint` annotations
when a server provides them, and verb heuristics when it does not. The rule that
matters: **a tool AEGIS cannot confidently call read-only counts as a write.**
Being asked about a harmless tool costs a click; auto-running an unrecognised one
can send mail as you.

You can answer "always allow this tool" on any prompt, which is remembered and
revocable on the Tools tab. Other policies: ask every time, run everything, or
per-server.

### Computer use

Off until you switch it on. Nine tools: screenshot, cursor position, click,
double-click, move, type, key, scroll, drag.

Screenshots are scaled to 1280px wide before they reach the model, and **every
click maps the model's coordinates back to real screen pixels** using the scale
from the last screenshot. Getting that wrong is why agents click 300 pixels off;
it is arithmetic, and it is tested.

Everything except screenshot and cursor-position is classified as a write, so
under the default policy each action asks first. `pyautogui`'s fail-safe stays
on — slam the pointer into the top-left corner to abort a run. There is no shell,
no file-delete and no window-close tool here; anything destructive is the job of
an MCP server you deliberately enabled.

**It needs a vision model.** Your local 3B models cannot read a screenshot. Route
computer use to Claude or GPT.

---

## Providers, honestly

### Local — nothing to configure
| Provider | Needs |
|---|---|
| **Ollama** | Ollama running on :11434 |
| **LM Studio** | LM Studio's local server switched on, :1234 |
| **llama.cpp (AEGIS)** | `llama-server.exe` in `%LOCALAPPDATA%\Aegis\bin` |

For GGUF files AEGIS downloads itself, it runs its own `llama-server`. AEGIS
does **not** fetch that binary for you — CUDA vs Vulkan vs plain AVX2 is a
decision you should make deliberately, so grab the right build from the
llama.cpp releases page and drop it in `bin`. Then "Load" appears on those model
cards.

### API keys — pay per token
OpenAI and Anthropic, encrypted on this machine. These are **separate from any
subscription you pay for** — an API key bills you again.

### Subscriptions — via the CLIs you already have
| Provider | Uses |
|---|---|
| **Codex CLI** | The ChatGPT login already inside `codex` |
| **Claude Code** | The Claude login already inside `claude` |

AEGIS shells out to them and streams the output into the same chat window.

**Why it works this way.** You asked for native OAuth sign-in for ChatGPT and
Claude. That option does not exist for third parties. The "Sign in with ChatGPT"
in Codex CLI and the equivalent in Claude Code use OAuth client IDs registered
to *those applications*. For AEGIS to replicate them it would have to present
someone else's client ID — which breaks both vendors' terms and stops working
the moment they rotate it. Reading `~/.codex/auth.json` and calling the backend
directly has the same problem wearing a different hat.

So AEGIS does the supported thing: the CLI holds its own session, refreshes its
own tokens, and AEGIS drives it. Same button in the UI, plumbing that will still
work next month.

### Browser sign-in — real OAuth, where it genuinely exists
The OAuth engine is complete: Authorization Code + PKCE (S256), loopback
redirect, encrypted token store, automatic refresh, no client secret anywhere.
Three providers are wired up:

| Provider | What it gets you |
|---|---|
| **Hugging Face** | Signed model downloads — required for gated repos like Llama and Gemma, and lifts anonymous rate limits |
| **Microsoft Entra** | GPT models through your own Azure OpenAI resource, authorised by your Microsoft account instead of a key |
| **Google** | Claude and Gemini through Vertex AI on your own GCP project |

Register this redirect URI exactly:

```
http://127.0.0.1:8817/oauth/callback
```

Then paste the client ID into the Providers tab. A client ID is not a secret in
a PKCE flow, but it is yours, so it lives in settings rather than in the source.

Note what the last two mean: you *can* reach first-party OpenAI and Anthropic
models through genuine third-party OAuth — just through the cloud platforms that
issue OAuth clients, rather than through consumer subscription endpoints that
do not.

---

## Where secrets live

| Platform | Backend |
|---|---|
| Windows | DPAPI (`CryptProtectData`), bound to your Windows account |
| Other | Fernet with a key file at `0600` |

On Windows, copying `vault.bin` to another machine or another user profile gives
them nothing, and there is no passphrase to type. Elsewhere the key file sits
beside it, so anything running as you can read it — stated plainly rather than
dressed up. Neither is a substitute for Bitwarden for credentials that matter.
This is a convenience store for machine-local API tokens.

---

## Needle: offline fast commands (v2.2)

[Needle](https://github.com/cactus-compute/needle) by Cactus Compute is a tiny
(about 14–29 MB) tool-calling model that runs on the CPU in milliseconds with no
network once it is cached. It cannot chat, reason or write, so AEGIS uses it
for one job only: spotting when a short message is really one of AEGIS's own
commands, before any model is asked.

| You type or say | Needle turns it into | A model is asked? |
|---|---|---|
| open youtube | `/browser https://youtube.com` | no |
| turn boost off | `/boost off` | no |
| switch to the job hunt project | `/project job hunt` | no |
| use the coder agent | `/agent coder` | no |
| start a new chat · show my files · read replies aloud · hands-free on | `/new` · `/files` · `/speak` · `/talk` | no |
| make an image of a lighthouse at dusk | `/image a lighthouse at dusk` | yes, with the right tool already chosen |
| search the web for SOC analyst jobs in London | `/search SOC analyst jobs in London` | yes |
| schedule a job search every weekday at 8am | `/schedule …` | yes |
| write me a cover letter… / why does my vCenter cert expire? | *(nothing — sent as usual)* | yes |

It works for voice too: hands-free mode goes through the same step. A toast
(⚡ Needle: /boost off · 91%) shows every time it acts.

**Fail closed.** Needle is only asked about short, single-line, non-code
messages with no attachments. Its answer is used only if it is exactly one
known command, its confidence is at or above the threshold (Settings, default
0.6), Needle has not flagged its own answer as ungrounded or negated, and any
project, agent or web address in it is something you actually typed. Any error,
or no answer within 1.5 s, and the message goes to the model unchanged. `/undo`
and `/computer` are deliberately not offered to Needle — type those yourself.

**Setup.** `cactus-needle` is in `requirements.txt`. To add it to an existing
install:

```
.venv\Scripts\python.exe -m pip install cactus-needle==3.0.4
```

(Pinned on purpose: 3.0.5 asks Hugging Face for engine 3.0.2, which Cactus
has not published yet, and fails with a 404.)

Then Settings → Behaviour → **Prepare Needle**. The first time downloads the
engine and weights (about 30 MB) from Hugging Face into
`%USERPROFILE%\.cache\cactus-needle`; after that it runs offline. Needle's
anonymous telemetry is switched off before it is loaded. Untick **Needle fast
commands** to turn the step off. `needle_weights` in settings can point at your
own fine-tuned `.cact` file (`needle finetune` / `needle build`).

Code: `aegis/needle_intent.py` (gates and command list), `aegis/web/needle.js`
(the hook in front of Send), `tests/test_v22.py`.

## Verify it yourself

```
.venv\Scripts\python.exe tests\test_aegis.py
.venv\Scripts\python.exe tests\test_v2.py
.venv\Scripts\python.exe tests\test_v21.py
.venv\Scripts\python.exe tests\test_v22.py
```

About 1,700 checks across the ten suites, all passing, no mocks where it
matters. `test_v2.py` covers every v2 promise above: each refusal kind failing
over and being remembered, a 401 resting only its provider, empty answers,
errors inside a 200 stream (against a real local HTTP server), overflow routes,
smart ordering, quick setup against a fake catalogue, Studio failover and the
Pollinations call shape, the coding tools' fence and refusals, web-fetch
refusing loopback and LAN, projects and scheduling from chat, the intent
classifier, and a whole run through the API inside a project.

**The MCP tests drive a real server process.** `tests/fake_mcp_server.py` is an
independent Python process speaking the actual wire protocol over stdio — real
JSON-RPC, real framing, real subprocess. The handshake, `tools/list`, tool calls
with arguments, image content blocks, `isError`, protocol errors and five
concurrent calls are all exercised against it. It is not a mock agreeing with
itself. It even prints a junk banner to stdout first, because real servers do,
and the client has to survive that.

The GGUF parser is tested against a file the suite builds byte by byte with a
known Llama-3.1-8B shape, asserting the KV cache comes out at exactly 512 MB at
4k. The fit calculator runs against three real hardware profiles — a 16 GB
Latitude with Iris Xe, a 24 GB RTX 4090, an 8 GB laptop — including the same 8B
model flipping green to red at 128k context.

**The persistence tests simulate the browser going away.** One starts a run,
consumes two events, abandons the stream, and proves the run finished anyway and
rejoining replays everything. Another starts a run with *no* subscriber, waits
for it to block on an approval, then attaches and answers it — proving an
approval survives a closed window. Image eviction is tested by squeezing the
budget to 1 MB and checking the oldest go and the newest stays.

**The platform gates are tested as gates.** A self-written skill is drafted and
then asserted to be absent from the index, absent from `skills.get()`, and the
agent's tool set is checked to contain `write` but not `approve`. A subagent is
run for real and its offered tool list is checked to contain no spawn tool. The
memory module is read as text and checked for write primitives, so "never writes
to your vault" is enforced rather than asserted.

Also covered: classification failing closed on unknown verbs, annotations
overriding names, every approval policy, live approve and deny through the async
path, the agent loop continuing after a denial, the step cap actually stopping an
endless loop, message serialisation for all three provider shapes, config
discovery with a check that env *values* never reach the UI, coordinate mapping
for computer use, secrets never hitting disk in plaintext, path traversal on the
delete endpoint being refused, and no consumer ChatGPT/Claude OAuth client
shipping in the codebase.

---

## Maturity

**fixture-tested** — up from artifact-verified. The MCP client is proven against
a real server process, and the agent loop, approval gate and classifier are
proven against scripted providers with deliberately adversarial cases.

v2 adds: the whole UI driven end to end in headless Chromium against a fake
free provider (Markdown rendering, failover chip, an image generated by a tool
call and shown in the chat, the Studio, projects, quick setup), and the
slideshow video fallback rendered for real with ffmpeg.

v2.2 adds Needle: its gates tested against a scripted engine that returns
cactus-needle 3.0.5's real envelope shape, and the Send/Enter hook driven in
headless Chromium (a command changes the setting without a model call; a normal
message still reaches the model; slash commands skip Needle). **Not** proven:
the real Needle engine on your PC — the build container could not download it
from Hugging Face. How accurate it is on your own phrasing is the thing to
check first; raise the confidence threshold if it ever grabs a message it
should not.

Still **not** proven for v2: live calls to Pollinations, OpenRouter, xKiro and
9Router (their exact error wording is matched by pattern, and new wordings may
need adding), the Windows offline voice, and `code__run` under real
PowerShell. Try each once after setup.

Still **not** proven: a live Ollama daemon, a real Hugging Face download, a real
OAuth round trip, a real `codex`/`claude` subprocess on Windows, a real
third-party MCP server such as the Drive or Gmail ones, and computer use against
an actual desktop (the container it was built in has no display, so the
screenshot and click paths are tested only as far as the coordinate arithmetic).
Those are the next things to prove, not things to assume from a green run.

One Windows-specific landmine handled in advance: stdio MCP servers are
subprocesses, and only the Proactor event loop can spawn those on Windows, so
`main.py` sets that policy before uvicorn builds its loop. Without it every stdio
server fails with `NotImplementedError`.

---

## What is not here yet

- **Deeper subagent trees.** Subagents can run four in parallel (v2) but still
  cannot spawn their own.
- **A free music model you can count on.** See the Studio section — the
  guaranteed fallback is a Suno pack, not audio.
- **Vault writing.** Deliberate, and reversible if you decide you want capture.
- **Semantic search.** Vault and memory search are keyword-based (FTS5). No
  embeddings, so a search for "certificate expiry" will not find a note that only
  says "PKI rotation".
- **Skill references.** Only `SKILL.md` is read. The `references/` folder in a
  skill is shipped but not loaded.
- **MCP resources and prompts.** Only `tools/*` is implemented. Servers that
  expose resources or prompt templates will have those ignored.
- **OAuth for remote MCP servers.** HTTP servers work with static headers; a
  server demanding its own OAuth dance is not handled yet.
- **Parallel tool calls.** Calls in one turn run in sequence, not concurrently.
- **Multi-model llama.cpp.** One `llama-server` at a time.
- **Packaging.** Runs from source via `run.bat`. No PyInstaller `.exe` yet.

---

## Layout

```
run.bat                  launcher, creates the venv on first run
aegis/
  main.py                entry point: API thread + WebView2 window
  server.py              FastAPI routes
  config.py              paths and settings
  vault.py               DPAPI / Fernet secret store
  hardware.py            hardware probe + the fit calculator
  gguf.py                GGUF header parser (local file and HTTP range)
  catalog.py             unified model list across all sources
  downloads.py           HF and Ollama downloads with progress and resume
  runners.py             llama-server process management
  oauth.py               OAuth 2.0 + PKCE engine
  agent.py               the tool loop
  router.py              ordered routes, health, cooldowns, failover
  files.py               attachment extraction and the searchable workspace
  scheduler.py           cron, Windows Task Scheduler, missed-run catch-up
  store.py               SQLite chat store, search, images, export
  runs.py                background runs, event buffering, reattach
  skills.py              skill loader, router index, gated self-authoring
  agents.py              agent roles and subagent spawning
  memory.py              agent facts + read-only Obsidian vault index
  skills_bundled/        the sixteen preinstalled skills (v2 adds coding,
                         research, media-studio, planner)
  taskroute.py           v2: request profiles and smart model ordering
  autoroute.py           v2: quick setup and the self-refreshing Auto route
  superagent.py          v2: core prompt, intent classifier, tool diet
  projects.py            v2: projects, workspaces, PROJECT.md, schedule tools
  media.py               v2: the Studio - backends, failover, gallery
  mcp/
    protocol.py          JSON-RPC, stdio and HTTP transports, MCP client
    discovery.py         read Codex / Claude Desktop / VS Code / Cursor configs
    registry.py          configured servers, connections, tool catalogue
  tools/
    base.py              tool shape, read/write classification, schemas
    approval.py          the approval gate and remembered decisions
    computer.py          screenshot, mouse and keyboard
    browser.py           page-level browser control, Chrome or own Chromium
    files.py             folder tools, fenced to what you connect (+ edit)
    code.py              v2: code__run and code__python
    web.py               v2: web__search and web__fetch
  providers/
    base.py              the contract every backend implements
    openai_compat.py     OpenAI, LM Studio, llama.cpp, Azure — one class
    anthropic.py         Messages API
    ollama_provider.py   native /api/chat
    cli.py               codex and claude subprocess delegation
    routed.py            a route wearing a Provider's clothes
    custom.py            bring-your-own OpenAI-compatible endpoints
  web/                   the UI: one HTML, one CSS, one JS, no build step
tests/
  test_aegis.py          entry point — runs everything, 640 checks
  test_mcp.py            MCP, tools, approvals, agent loop
  test_store.py          persistence, reattach, image eviction
  test_platform.py       skills, self-authoring gate, memory, subagents
  test_router.py         classification, cooldowns, failover, free/paid wall
  test_files.py          extraction, the fence, cron, catch-up
  fake_mcp_server.py     a real MCP server, for real protocol tests
```

Rename it by changing `APP_NAME` in `config.py`.
