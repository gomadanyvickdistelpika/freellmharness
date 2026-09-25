# Contributing to AEGIS (freellmharness)

Thanks for helping! Testers, bug reports, docs fixes and code are all welcome.
You don't need to be an expert: issues labelled
[`good first issue`](https://github.com/gomadanyvickdistelpika/freellmharness/labels/good%20first%20issue)
are picked to be doable in an evening.

## Ways to help

- **Test it** on your Windows PC and [open a bug report](../../issues/new/choose) for anything
  that breaks or confuses you. "The README said X but I saw Y" is a great report.
- **Suggest improvements** with the *Idea or improvement* template, or start a
  [Discussion](../../discussions).
- **Fix something** — pick an open issue, comment "I'll take this", and send a pull request.

## Run it from source (5 minutes)

Windows 10/11 with Python 3.10–3.12:

```powershell
git clone https://github.com/gomadanyvickdistelpika/freellmharness.git
cd freellmharness
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m aegis          # starts the app
```

Linux/macOS work for development and tests (the desktop window and computer use are
Windows-first): use `python3 -m venv .venv` and `.venv/bin/python`.

## Run the tests

Every suite is a plain script — no pytest needed:

```powershell
.venv\Scripts\python.exe tests\test_aegis.py
.venv\Scripts\python.exe tests\test_v2.py
.venv\Scripts\python.exe tests\test_v21.py
.venv\Scripts\python.exe tests\test_v22.py
```

The last line of each prints `N passed, 0 failed`. The same suites run automatically on
every pull request (GitHub Actions, Windows). Tests write to a temporary data folder, never to
your real `%LOCALAPPDATA%\Aegis`.

## Where things live

| Folder | What |
|---|---|
| `aegis/server.py` | The local API (FastAPI) behind the window |
| `aegis/agent.py`, `superagent.py` | The agent loop and the routing/prompt layer |
| `aegis/router.py`, `taskroute.py`, `providers/` | Model routes, failover, provider adapters |
| `aegis/tools/` | Built-in tools (files, code, web, browser, computer) and the approval gate |
| `aegis/skills_bundled/` | The skill pack (Markdown) — easy first contributions |
| `aegis/personal_bundled/` | The "About me" templates new users fill in |
| `aegis/web/` | The UI (plain HTML/CSS/JS, no build step) |
| `tests/` | One script per area; `harness.py` is the tiny pass/fail helper |
| `docs/TECHNICAL.md` | Deep dive: routing, failover, MCP, privacy tiers |

## Ground rules for changes

1. **Safety first.** Anything that writes files, clicks, sends, spends money or shares private
   notes must go through the approval gate. PRs that bypass it won't be merged.
2. **Never commit secrets or personal data.** Use `tests/demo_profile.py` (a fictional user)
   in tests.
3. **Add or update a test** for behaviour you change. Honest tests only — a test that can't
   fail isn't measuring anything.
4. **Keep it Windows-friendly**: `.bat`/`.ps1` files keep CRLF line endings (`.gitattributes`
   handles it).
5. Small, focused pull requests are reviewed fastest.

## Pull request steps

1. Fork the repo and create a branch: `git checkout -b fix-short-description`.
2. Make the change and run the tests.
3. Push and open a pull request; the template asks how you tested it.
4. The maintainer reviews every PR before merging. Be patient and kind — this is a
   one-person project so far.

By contributing you agree your work is released under the [MIT licence](LICENSE) and that you
will follow the [Code of Conduct](CODE_OF_CONDUCT.md).
