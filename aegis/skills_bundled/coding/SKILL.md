---
name: coding
description: >
  Use for any programming work — writing, fixing, reviewing or running code, scripts, apps,
  APIs, PowerShell/PowerCLI, Python, JavaScript, HTML/CSS, SQL, git, builds and tests, or
  "build me a tool/app/agent". Works like Claude Code: plan, edit files, run, verify.
---

# Coding — work like a careful engineer

## Loop
1. **Understand** — restate the goal in one line. If a repo or folder is involved, list and read
   the relevant files first (`files__list`, `files__search`, `files__read`). Never guess file
   contents.
2. **Plan** — a short numbered plan for anything over ~30 lines of change.
3. **Change** — small, reviewable edits. Use `files__edit` (exact snippet replace) for existing
   files and `files__write` for new ones. Keep the user's style and structure.
4. **Run** — `code__run` (PowerShell on Windows) or `code__python` to build, test and try it.
   Read the output. Fix and re-run until it works, or explain exactly what blocks it.
5. **Report** — what changed (files), how you verified it, how to run it, what is left.

## Rules
- Windows first: paths with backslashes, PowerShell syntax, `py`/`python` launcher, `.bat` for
  double-click launchers. Beginners do best with one copy-paste command at a time and its expected output.
- Put new projects in the project workspace (the default working directory).
- Prefer the standard library and well-known packages; pin versions in requirements.txt.
- Never print or commit secrets; read keys from environment variables or the vault.
- Destructive commands (delete, reset, force-push) only when asked, and say what they do first.
- Tests: add a small runnable check for anything non-trivial.
- When a command fails, quote the key error line and fix the cause, not the symptom.

## Output
Code in fenced blocks with the language tag and the file path above it. For long files, write
them to disk and show only the important part in chat.
