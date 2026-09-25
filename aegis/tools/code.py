"""Coding tools: run commands and Python in the workspace, like Claude Code.

    code__run      a shell command (PowerShell on Windows, bash elsewhere)
    code__python   a Python snippet, run with AEGIS's own interpreter

Both are writes, so under the default policy every call asks first and shows
you the exact command. Both are fenced: the working directory must resolve
inside the current project's workspace, a connected folder, or the general
AEGIS workspace. A command can of course reach further than its working
directory - that is what the approval prompt is for - but a small set of
obviously catastrophic commands is refused outright even if approved, because
one mis-click should not be able to format a drive.

Output is captured (stdout + stderr), trimmed to the head and tail when long,
and the exit code is reported so the model can tell success from failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .base import READ, WRITE, Tool

MAX_OUTPUT = 30_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900

# Refused even when approved. Deliberately short: these have no legitimate use
# from a chat, and each is one keystroke from losing a disk.
_CATASTROPHIC = re.compile(
    r"(\bformat(\.com)?\s+[a-z]:|\bdiskpart\b|\bmkfs\b|\bdd\s+if=.*of=/dev/|"
    r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\*|/\*)\s*$|\brm\s+-rf\s+/(\s|$)|"
    r"remove-item\s+.*-recurse.*\s[a-z]:\\\\?\s*($|-)|"
    r"\bdel\s+/[sq].*\s[a-z]:\\\\?\*?\s*$|\brd\s+/s\s+/q\s+[a-z]:\\\\?\s*$|"
    r"\bshutdown\b|\bbcdedit\b|\bvssadmin\s+delete|\bcipher\s+/w|"
    r"reg\s+delete\s+hk(lm|ey_local_machine))", re.IGNORECASE)


def _default_cwd() -> Path:
    from .. import projects
    key = projects.current_key()
    if key and projects.get(key):
        return projects.workspace(key)
    return projects.general_workspace()


def _resolve_cwd(raw: str) -> Path:
    from .files import resolve
    if not raw:
        return _default_cwd()
    target = resolve(raw)
    if not target.is_dir():
        raise ValueError(f"{target} is not a folder")
    return target


def _trim(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    head = text[: MAX_OUTPUT // 2]
    tail = text[-MAX_OUTPUT // 2:]
    return f"{head}\n\n… [{len(text) - MAX_OUTPUT:,} characters cut] …\n\n{tail}"


async def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def execute(argv: list[str], cwd: Path, timeout: float,
                  stdin: bytes | None = None) -> dict[str, Any]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUTF8"] = "1"
    started = time.time()
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **kwargs)
    except (OSError, NotImplementedError) as exc:
        return {"text": f"Could not start {argv[0]}: {exc}", "error": True}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
        timed_out = False
    except asyncio.TimeoutError:
        await _kill(proc)
        out = b""
        timed_out = True
    text = (out or b"").decode("utf-8", errors="replace")
    seconds = round(time.time() - started, 1)
    if timed_out:
        return {"text": f"[timed out after {timeout:.0f}s and was stopped]\n{text}",
                "error": True}
    code = proc.returncode
    header = f"[exit {code} · {seconds}s · {cwd}]"
    return {"text": f"{header}\n{_trim(text) or '(no output)'}",
            "error": code != 0}


async def run_command(command: str = "", cwd: str = "", timeout: int = 0,
                      **_: Any) -> dict[str, Any]:
    command = (command or "").strip()
    if not command:
        return {"text": "A command is required.", "error": True}
    if _CATASTROPHIC.search(command):
        return {"text": "Refused: that command could destroy data or the "
                        "system, so AEGIS will not run it from a chat even "
                        "with approval. Run it yourself if you really mean it.",
                "error": True}
    folder = _resolve_cwd(cwd)
    limit = max(5, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    if sys.platform == "win32":
        argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    else:
        argv = ["bash", "-lc", command]
    return await execute(argv, folder, limit)


async def run_python(code: str = "", cwd: str = "", timeout: int = 0,
                     **_: Any) -> dict[str, Any]:
    if not (code or "").strip():
        return {"text": "Some code is required.", "error": True}
    folder = _resolve_cwd(cwd)
    scratch = folder / ".aegis-run"
    scratch.mkdir(exist_ok=True)
    script = scratch / f"snippet-{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")
    limit = max(5, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    try:
        return await execute([sys.executable, str(script)], folder, limit)
    finally:
        try:
            script.unlink()
        except OSError:
            pass


GIT_READ = {"status", "diff", "log", "show", "branch", "blame", "ls-files",
            "rev-parse", "remote", "shortlog", "describe", "tag", "stash"}


async def run_git(args: str = "", cwd: str = "", **_: Any) -> dict[str, Any]:
    """Read-only git. Anything that changes the repo goes through code__run."""
    import shlex
    parts = shlex.split(args or "status", posix=True)
    sub = parts[0] if parts else "status"
    if sub not in GIT_READ or (sub == "stash" and parts[1:2] not in ([], ["list"])) \
            or (sub in ("branch", "tag", "remote") and any(
                p in ("-d", "-D", "--delete", "-m", "-M", "add", "remove", "rm",
                      "rename", "set-url") for p in parts[1:])):
        return {"text": f"code__git only runs read-only commands ({', '.join(sorted(GIT_READ))}). "
                        "Commit, checkout, push and friends go through code__run, "
                        "which asks first.", "error": True}
    folder = _resolve_cwd(cwd)
    return await execute(["git", "--no-pager", *parts], folder, 60)


def _wrap(fn):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from .files import OutsideFence
        try:
            return await fn(**args)
        except OutsideFence as exc:
            return {"text": f"Refused: {exc}", "error": True}
        except Exception as exc:
            return {"text": f"{type(exc).__name__}: {exc}", "error": True}
    return handler


def tools() -> list[Tool]:
    from ..config import settings
    if not settings.get("code_tools_enabled", True):
        return []
    shell = "PowerShell" if sys.platform == "win32" else "bash"
    return [
        Tool(name="code__run", raw_name="run",
             description=(
                 f"Run a {shell} command and get its output and exit code. "
                 "Use it to build, test, install packages (pip/npm), run git, "
                 "inspect the system. Working directory defaults to the "
                 "project workspace. Asks the user first. Prefer small, "
                 "verifiable steps; read errors and fix them."),
             input_schema={"type": "object", "properties": {
                 "command": {"type": "string"},
                 "cwd": {"type": "string",
                         "description": "folder inside the workspace or a "
                                        "connected folder"},
                 "timeout": {"type": "integer",
                             "description": f"seconds, default {DEFAULT_TIMEOUT}, "
                                            f"max {MAX_TIMEOUT}"}},
                 "required": ["command"]},
             server="code", origin="builtin", risk=WRITE,
             handler=_wrap(run_command)),
        Tool(name="code__git", raw_name="git",
             description=("Read-only git: status, diff, log, show, branch, blame, "
                          "ls-files. Runs without asking. Use it to understand a "
                          "repo before changing it and to review your own diff. "
                          "Commits/checkouts/pushes go through code__run."),
             input_schema={"type": "object", "properties": {
                 "args": {"type": "string",
                          "description": "e.g. 'status', 'diff --stat', 'log --oneline -10'"},
                 "cwd": {"type": "string"}}},
             server="code", origin="builtin", risk=READ,
             handler=_wrap(run_git)),
        Tool(name="code__python", raw_name="python",
             description=(
                 "Run a Python snippet and get its printed output. Good for "
                 "calculations, data wrangling (csv/xlsx/json), quick checks, "
                 "and generating files into the workspace. Asks first."),
             input_schema={"type": "object", "properties": {
                 "code": {"type": "string"},
                 "cwd": {"type": "string"},
                 "timeout": {"type": "integer"}},
                 "required": ["code"]},
             server="code", origin="builtin", risk=WRITE,
             handler=_wrap(run_python)),
    ]
