"""Subscription routes: drive the CLIs you already have signed in.

This is the honest way to use a ChatGPT or Claude subscription from your own
harness. Codex CLI and Claude Code each hold their own OAuth session, refresh
their own tokens, and are supported by their vendor. AEGIS shells out to them
and streams the output back into the same chat window as every other provider.

What it deliberately does NOT do: read ~/.codex/auth.json or Claude's
credentials file and call the backend API directly. That impersonates a
first-party client, breaks both vendors' terms, and stops working the next time
they rotate a client id.

Each run gets a scratch working directory. Codex wants a git repo, so it gets
one. Nothing here touches your real projects unless you point workdir at them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

from .base import Event, Message, Provider, flatten_for_text, not_ok, ok

_WINDOWS = sys.platform == "win32"


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if _WINDOWS:
        for ext in (".cmd", ".exe", ".bat"):
            if found := shutil.which(name + ext):
                return found
    return None


def _flatten(messages: list[Message]) -> str:
    """These CLIs take one prompt, so the conversation is folded into text.

    They run their own tool loops with their own MCP servers, so AEGIS tools are
    not offered here; any tool traffic already in the transcript is rendered as
    text rather than dropped.
    """
    messages = flatten_for_text(messages)
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[Instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant reply]\n{content}")
        else:
            parts.append(content if len(messages) == 1 else f"[User]\n{content}")
    return "\n\n".join(parts)


class _CLIProvider(Provider):
    kind = "cli"
    needs = "cli"
    binary = ""
    install_hint = ""

    def _workdir(self, opts: dict[str, Any]) -> Path:
        wd = opts.get("workdir")
        if wd and Path(wd).is_dir():
            return Path(wd)
        return Path(tempfile.mkdtemp(prefix="aegis-cli-"))

    async def status(self) -> dict[str, Any]:
        path = _which(self.binary)
        if not path:
            return not_ok(f"`{self.binary}` not found on PATH", self.install_hint)
        return ok(f"Found at {path}")

    async def models(self) -> list[str]:
        return ["(whatever the CLI is configured to use)"]

    def _command(self, prompt: str, model: str, workdir: Path) -> list[str]:
        raise NotImplementedError

    def _prepare(self, workdir: Path) -> None:
        pass

    def _parse_line(self, line: str) -> Event | None:
        return {"type": "delta", "text": line + "\n"}

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        exe = _which(self.binary)
        if not exe:
            yield {"type": "error",
                   "text": f"`{self.binary}` is not installed or not on PATH. "
                           f"{self.install_hint}"}
            return

        workdir = self._workdir(opts)
        try:
            self._prepare(workdir)
        except Exception as exc:
            yield {"type": "error", "text": f"Could not prepare workspace: {exc}"}
            return

        prompt = _flatten(messages)
        cmd = [exe, *self._command(prompt, model, workdir)[1:]]
        yield {"type": "notice",
               "text": f"Running {self.binary} in {workdir}\n"}

        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir), env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000 if _WINDOWS else 0,
            )
        except Exception as exc:
            yield {"type": "error", "text": f"Could not start {self.binary}: {exc}"}
            return

        assert proc.stdout
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                event = self._parse_line(line)
                if event:
                    yield event
            code = await proc.wait()
            if code != 0:
                yield {"type": "error",
                       "text": f"{self.binary} exited with code {code}"}
                return
            yield {"type": "done"}
        except asyncio.CancelledError:
            proc.kill()
            raise


class CodexCLIProvider(_CLIProvider):
    key = "codex_cli"
    label = "Codex CLI (ChatGPT sign-in)"
    binary = "codex"
    blurb = ("Uses the ChatGPT login already in Codex CLI. Best for real code "
             "changes and repo work.")
    install_hint = "Install with: npm i -g @openai/codex, then run `codex` once to sign in."

    def _prepare(self, workdir: Path) -> None:
        # Codex expects a git repo. Give the scratch dir one so it does not bail.
        if not (workdir / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=str(workdir),
                           capture_output=True, timeout=20,
                           creationflags=0x08000000 if _WINDOWS else 0)

    def _command(self, prompt: str, model: str, workdir: Path) -> list[str]:
        cmd = [self.binary, "exec", "--skip-git-repo-check"]
        if model and not model.startswith("("):
            cmd += ["--model", model]
        cmd.append(prompt)
        return cmd


class ClaudeCLIProvider(_CLIProvider):
    key = "claude_cli"
    label = "Claude Code (Claude sign-in)"
    binary = "claude"
    blurb = ("Uses the Claude login already in Claude Code. Good for long "
             "reasoning and writing with tools.")
    install_hint = ("Install with: npm i -g @anthropic-ai/claude-code, "
                    "then run `claude` once to sign in.")

    def _command(self, prompt: str, model: str, workdir: Path) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json",
               "--verbose"]
        if model and not model.startswith("("):
            cmd += ["--model", model]
        return cmd

    def _parse_line(self, line: str) -> Event | None:
        line = line.strip()
        if not line:
            return None
        try:
            evt = json.loads(line)
        except ValueError:
            return {"type": "notice", "text": line + "\n"}

        etype = evt.get("type")
        if etype == "assistant":
            blocks = ((evt.get("message") or {}).get("content")) or []
            text = "".join(b.get("text", "") for b in blocks
                           if isinstance(b, dict) and b.get("type") == "text")
            tools = [b.get("name") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
            if tools:
                return {"type": "notice",
                        "text": f"[tool: {', '.join(t for t in tools if t)}]\n"}
            return {"type": "delta", "text": text} if text else None
        if etype == "result":
            usage = evt.get("usage") or {}
            return {"type": "usage",
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0)}
        return None
