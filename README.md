# freellmharness — AEGIS, a free AI assistant for your Windows PC

AEGIS is one window on your PC that talks to **free** AI models (and paid ones if you
want). It has chat, voice, image and song making, a built-in browser, file uploads,
coding help, scheduled tasks and ready-made "agents" like a Coder, a Job Hunter and a
Researcher. When one free model runs out, it quietly switches to the next.

It starts knowing **nothing about you**. You tell it about yourself in the **Me** tab, and
that information stays on your PC.

> **This is a test version shared between friends.** It works, but expect rough edges.
> Tell whoever sent you the link what broke. See [Giving feedback](#giving-feedback).

---

## What you need

- A **Windows 10 or 11** PC. Any normal laptop is fine; you don't need a gaming graphics card.
- About **3 GB of free disk space**.
- **20 minutes** the first time.
- **Python 3.12**. The steps below show you how to get it.
- At least one **free AI key**. Step 4 shows you how (about 2 minutes).

---

## Install it: step by step

### Step 1: Install Python (skip this if you already have it)

1. Go to **https://www.python.org/downloads/** and click the big yellow
   **Download Python 3.12…** button. (Any 3.10, 3.11 or 3.12 works.)
2. Open the file you downloaded.
3. ⚠️ **On the first screen, tick the box "Add python.exe to PATH".** This is the step
   people miss.
4. Click **Install Now** and wait for "Setup was successful". Close it.

### Step 2: Download AEGIS

1. On this GitHub page, click the green **`<> Code`** button, then **Download ZIP**.
2. Open your **Downloads** folder, right-click **`freellmharness-main.zip`**, then
   **Extract All… → Extract**.
3. You now have a folder called **`freellmharness-main`**. Open it.

### Step 3: Run the setup

1. Double-click **`AEGIS-SETUP.bat`**.
2. If a blue box says **"Windows protected your PC"**, click **More info → Run anyway**.
   (It says this about any program downloaded from the internet that isn't signed.)
3. A black window shows steps **[1/7] to [7/7]**. It takes about **5–10 minutes**. Leave it
   alone until it says **"Finished. Starting AEGIS now."**
4. AEGIS opens. There's now an **AEGIS icon on your Desktop** for next time.

> If it stops with a **PROBLEM:** message, read the **FIX:** line under it. Most often
> Python wasn't added to PATH: reinstall it and tick the box, then double-click
> `AEGIS-SETUP.bat` again. It's safe to run more than once.

### Step 4: Give it a free AI key (about 2 minutes)

AEGIS needs at least one AI provider. These are free and need no credit card:

| Provider | Where to get the key | Good for |
|---|---|---|
| **Google AI Studio (Gemini)** | https://aistudio.google.com/apikey → **Create API key** | Best all-rounder; can see images |
| **Groq** | https://console.groq.com/keys → **Create API Key** | Very fast answers |
| **OpenRouter** | https://openrouter.ai/keys → **Create Key** | Many free models in one place |

1. Create a key on one of those sites and **copy** it. It's a long line of letters and
   numbers.
2. In AEGIS, click the **Providers** tab. The first box is **Quick setup**.
3. **Paste** the key into the matching box and click **Save keys and build my Auto route**.
4. Go back to **Chat** and type *hello*. You should get an answer. 🎉

Your keys are stored **encrypted on your PC only**. They are never uploaded anywhere
except to the provider they belong to.

> **No internet or don't want a key?** Install **Ollama** (https://ollama.com), then in
> AEGIS go to **Models** and download a small model such as `qwen3:4b`. It runs fully
> offline, just slower.

### Step 5: Tell it about yourself (5 minutes, recommended)

A blue **Welcome** bar asks you to do this the first time.

1. Click the **Me** tab.
2. Click **about** in the list on the left. Replace everything in `{{double curly brackets}}` with your own details:
   name, city and country, languages, and a short summary. Click **Save**.
3. Do the same for **working-style**: how you like answers (short or detailed, one step at
   a time, and so on).
4. Fill in the others only if you want them: **career** and **job-search** (for CVs and
   cover letters), **interests**, **tech-setup**, and the private ones (**family**,
   **health-fitness**, **money-admin**).

What each note's privacy level means:

- **public** notes (about, working-style, interests…) go with every message.
- **personal** notes (career, job-search) are only looked up when a task needs them.
- **private** notes (family, health, money) are **never** sent to an online AI without
  asking you first. You'll see a *"share this with the model?"* box and choose.

You can leave any note unfilled. AEGIS never treats `{{placeholders}}` as facts. It asks you
instead.

---

## Things to try

Type these in **Chat**:

- `write me a short cover letter for a barista job` (better once your **career** note is filled in)
- `/image a lighthouse at dusk, watercolour`
- `/search best free budgeting apps this year`
- `open youtube.com`: this opens the built-in browser straight away, without asking a model
- `/code make me a python script that renames my photos by date`
- `/agent researcher`, then ask a question. Agents are specialists with their own instructions.
- Click 🎤 and talk. **Right-click** 🎤 for hands-free mode, where it answers out loud.

Type `/` to see every command.

### Optional: instant offline commands (Needle)

Short commands like *"turn boost off"* or *"start a new chat"* can run instantly on your PC
without asking an AI model. Go to **Settings → Behaviour** and click **Prepare Needle**. It
downloads about 30 MB once.

---

## Make it yours

| I want to… | Do this |
|---|---|
| Change what it knows about me | **Me** tab: edit or add notes |
| Change how it answers | **Me → working-style** |
| Make my own specialist (e.g. "Recipe helper") | **Agents** tab → fill in **New agent**, or type *"make me an agent that…"* in chat |
| Change a skill's instructions | Copy a folder from `aegis\skills_bundled\` into `%LOCALAPPDATA%\Aegis\skills\` and edit the copy. Yours wins. |
| Add a new skill | Ask in chat: *"write a skill for…"*. It waits on the **Skills** tab until you approve it. |
| Connect Gmail, GitHub, Drive… | **Agents** tab → MCP catalogue → **Install** |
| Run something every morning | **Tasks** tab, or type *"every weekday at 8am, …"* |
| Use only offline AI | Install Ollama, download a model, choose it in the chat bar |

17 skills come built in: coding, research, planning, media, job search, money, health,
family and school, immigration, forms, home and car, music, fitness, building agents,
VMware support and more. They're written for **anyone**; your personal details come from
your **Me** notes. See [`aegis/skills_bundled/PACK.md`](aegis/skills_bundled/PACK.md).

---

## Privacy: what leaves your PC

- **Nothing about you is in this download.** Your notes, chats, keys and files are created
  on your PC in `%LOCALAPPDATA%\Aegis`.
- Your messages go to the AI provider you chose (Gemini, Groq…). Free providers may use
  prompts to improve their models, so don't paste passwords or bank details into chat.
- Actions that change things (writing files, clicking in the browser, sending) **ask you
  first** by default.
- To remove everything: delete the `freellmharness-main` folder and `%LOCALAPPDATA%\Aegis`.

---

## Problems?

| What you see | Fix |
|---|---|
| "Python is not installed, or not on your PATH" | Reinstall Python and **tick "Add python.exe to PATH"**. Run `AEGIS-SETUP.bat` again. |
| "Windows protected your PC" | **More info → Run anyway** |
| Chat says no provider / no model | Step 4: paste a free key in **Providers → Quick setup** |
| Answers stop with "rate limit" | Free limits reached; add a second free key so it can switch |
| The window doesn't open | Double-click `run.bat` in the folder. The black window shows the error. |
| Anything else | Look at `aegis-setup.log` in the folder and send it with your feedback |

---

## Giving feedback

Please tell the person who shared this with you:

1. What you tried to do.
2. What happened instead (a screenshot helps).
3. The file `aegis-setup.log` from the folder, if setup went wrong.

If you have a GitHub account, you can also open an **Issue** on this page.

---

## For the technically curious

Everything else (model routing and failover, the can-it-run gauge, MCP, the agent loop, the
approval gate, tests) is in [`docs/TECHNICAL.md`](docs/TECHNICAL.md).
Run the tests with `.venv\Scripts\python.exe tests\test_aegis.py`.

MIT licence. See [LICENSE](LICENSE).
