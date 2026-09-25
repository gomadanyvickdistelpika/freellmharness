"""Scheduled tasks — a daily job search that happens without you.

Two layers, because either one alone is wrong:

  in-app       a loop that fires due tasks while AEGIS is open. Cheap, exact,
               and useless if the app is closed at 8am.
  Windows      a real Task Scheduler entry that wakes AEGIS headless, runs one
               task and exits. Survives reboots and a closed app, and needs the
               user to approve its creation.

Missed runs are caught up once on launch: if yesterday's 8am run never happened
because the laptop was shut, it runs when you next open AEGIS rather than
silently skipping a day. Once, not once per missed day - waking up to fourteen
identical job searches helps nobody.

Scheduled runs are unattended by definition, so anything needing approval is
declined immediately rather than stalling on a prompt no one will answer. A task
can be marked trusted, which auto-approves instead.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DATA_DIR, settings

TASK_PREFIX = "AEGIS"
CHECK_EVERY = 30.0          # seconds between in-app checks
MAX_CATCHUP_AGE = 3 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

def _field(spec: str, low: int, high: int) -> set[int]:
    """Parse one cron field: *, a, a-b, a-b/n, */n, and comma lists of those."""
    values: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            step = max(1, int(raw_step))
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < low or end > high or start > end:
            raise ValueError(f"{spec!r} is out of range for {low}-{high}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"{spec!r} matches nothing")
    return values


@dataclass
class Cron:
    minute: set[int]
    hour: set[int]
    dom: set[int]
    month: set[int]
    dow: set[int]
    expr: str = ""

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        # cron's day-of-week is 0-6 with Sunday 0; python's weekday() is Mon 0.
        weekday = (when.weekday() + 1) % 7
        dom_any = self.dom == set(range(1, 32))
        dow_any = self.dow == set(range(0, 7))
        if dom_any and dow_any:
            return True
        if dom_any:
            return weekday in self.dow
        if dow_any:
            return when.day in self.dom
        # Both restricted: cron treats that as OR.
        return when.day in self.dom or weekday in self.dow


def parse_cron(expr: str) -> Cron:
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise ValueError("A cron expression needs 5 fields: "
                         "minute hour day-of-month month day-of-week")
    return Cron(minute=_field(parts[0], 0, 59), hour=_field(parts[1], 0, 23),
                dom=_field(parts[2], 1, 31), month=_field(parts[3], 1, 12),
                dow=_field(parts[4], 0, 6), expr=expr.strip())


def next_run(expr: str, after: datetime | None = None) -> datetime | None:
    """The next minute at or after `after` that the expression matches."""
    try:
        cron = parse_cron(expr)
    except ValueError:
        return None
    moment = (after or datetime.now()).replace(second=0, microsecond=0)
    moment += timedelta(minutes=1)
    # A year of minutes is the worst case for something like 0 0 29 2 *.
    for _ in range(366 * 24 * 60):
        if cron.matches(moment):
            return moment
        moment += timedelta(minutes=1)
    return None


def previous_run(expr: str, before: datetime | None = None) -> datetime | None:
    """The most recent matching minute at or before `before`."""
    try:
        cron = parse_cron(expr)
    except ValueError:
        return None
    moment = (before or datetime.now()).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        if cron.matches(moment):
            return moment
        moment -= timedelta(minutes=1)
    return None


def describe(expr: str) -> str:
    """A plain-English rendering, for the ones worth naming."""
    try:
        parse_cron(expr)
    except ValueError as exc:
        return f"invalid: {exc}"
    parts = expr.split()
    minute, hour, dom, month, dow = parts
    if dom == "*" and month == "*" and minute.isdigit() and hour.isdigit():
        at = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"every day at {at}"
        if dow == "1-5":
            return f"every weekday at {at}"
        names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday"]
        try:
            days = sorted(_field(dow, 0, 6))
            return f"every {', '.join(names[d] for d in days)} at {at}"
        except ValueError:
            pass
    if minute.startswith("*/") and hour == "*":
        return f"every {minute[2:]} minutes"
    return f"cron: {expr}"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@dataclass
class Task:
    key: str
    name: str
    prompt: str
    cron: str = "0 8 * * 1-5"
    provider: str = ""             # "route:test" or a provider key
    model: str = ""
    enabled: bool = True
    use_tools: bool = True
    trusted: bool = False          # auto-approve writes on an unattended run
    output_dir: str = ""
    project: str = ""              # v2: run inside this project's workspace
    agent: str = ""                # v2.1: run as one of your agents
    catch_up: bool = True
    windows_task: bool = False     # registered with Task Scheduler
    last_run: float = 0.0
    last_status: str = ""
    last_chat_id: str = ""
    last_file: str = ""
    runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        upcoming = next_run(self.cron)
        d["schedule_text"] = describe(self.cron)
        d["next_run"] = upcoming.timestamp() if upcoming else 0
        d["next_run_text"] = upcoming.strftime("%a %d %b, %H:%M") if upcoming else "never"
        return d


def tasks() -> dict[str, Task]:
    out: dict[str, Task] = {}
    for key, raw in (settings.get("tasks") or {}).items():
        if not isinstance(raw, dict):
            continue
        fields = {k: v for k, v in raw.items() if k in Task.__dataclass_fields__}
        fields["key"] = key
        try:
            out[key] = Task(**fields)
        except TypeError:
            continue
    return out


def get(key: str) -> Task | None:
    return tasks().get(key)


def save(task: Task) -> Task:
    stored = dict(settings.get("tasks") or {})
    payload = asdict(task)
    payload.pop("key", None)
    stored[task.key] = payload
    settings.set("tasks", stored)
    return task


def create(name: str, prompt: str, cron: str, **kwargs: Any) -> dict[str, Any]:
    if not name.strip() or not prompt.strip():
        return {"ok": False, "error": "A name and a prompt are both required."}
    try:
        parse_cron(cron)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    key = re.sub(r"[^a-z0-9-]", "-", name.strip().lower())[:40].strip("-")
    key = key or uuid.uuid4().hex[:8]
    if key in tasks():
        key = f"{key}-{uuid.uuid4().hex[:4]}"

    allowed = {k: v for k, v in kwargs.items() if k in Task.__dataclass_fields__}
    task = Task(key=key, name=name.strip(), prompt=prompt.strip(),
                cron=cron.strip(), **allowed)
    save(task)
    return {"ok": True, "key": key, "task": task.to_dict()}


def delete(key: str) -> bool:
    stored = dict(settings.get("tasks") or {})
    existed = key in stored
    stored.pop(key, None)
    settings.set("tasks", stored)
    unregister_windows(key)
    return existed


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def task_name(key: str) -> str:
    return f"{TASK_PREFIX} - {key}"


def _schtasks_schedule(cron: str) -> list[str]:
    """Translate the common cron shapes into schtasks arguments."""
    minute, hour, dom, month, dow = cron.split()
    if not (minute.isdigit() and hour.isdigit()):
        raise ValueError("Windows Task Scheduler needs a fixed time of day. "
                         "Use a cron like '30 8 * * 1-5'.")
    at = f"{int(hour):02d}:{int(minute):02d}"

    if dow == "*" and dom == "*":
        return ["/SC", "DAILY", "/ST", at]
    if dow != "*":
        names = {0: "SUN", 1: "MON", 2: "TUE", 3: "WED", 4: "THU",
                 5: "FRI", 6: "SAT"}
        days = sorted(_field(dow, 0, 6))
        return ["/SC", "WEEKLY", "/D", ",".join(names[d] for d in days), "/ST", at]
    if dom != "*":
        days = sorted(_field(dom, 1, 31))
        return ["/SC", "MONTHLY", "/D", ",".join(str(d) for d in days), "/ST", at]
    return ["/SC", "DAILY", "/ST", at]


def register_windows(key: str) -> dict[str, Any]:
    """Ask Windows to wake AEGIS for this task even when it is closed."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Task Scheduler only exists on Windows. "
                                      "The in-app scheduler still works."}
    task = get(key)
    if not task:
        return {"ok": False, "error": f"No task {key!r}."}
    try:
        schedule = _schtasks_schedule(task.cron)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    command = f'"{_pythonw()}" -m aegis --run-task {key}'
    args = ["schtasks", "/Create", "/TN", task_name(key), "/TR", command,
            *schedule, "/F"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30,
                              creationflags=0x08000000)
    except Exception as exc:
        return {"ok": False, "error": f"Could not run schtasks: {exc}"}

    if proc.returncode != 0:
        return {"ok": False,
                "error": (proc.stderr or proc.stdout or "schtasks failed").strip()[:400]}

    task.windows_task = True
    save(task)
    return {"ok": True, "task_name": task_name(key), "command": command,
            "schedule": describe(task.cron)}


def unregister_windows(key: str) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": True, "note": "nothing to remove off Windows"}
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", task_name(key), "/F"],
                       capture_output=True, text=True, timeout=30,
                       creationflags=0x08000000)
    except Exception:
        pass
    if task := get(key):
        task.windows_task = False
        save(task)
    return {"ok": True}


def windows_status(key: str) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"registered": False, "available": False}
    try:
        proc = subprocess.run(["schtasks", "/Query", "/TN", task_name(key)],
                              capture_output=True, text=True, timeout=20,
                              creationflags=0x08000000)
        return {"registered": proc.returncode == 0, "available": True}
    except Exception:
        return {"registered": False, "available": True}


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def output_folder(task: Task) -> Path:
    if task.output_dir:
        folder = Path(task.output_dir).expanduser()
    elif task.project:
        from . import projects
        folder = (projects.workspace(task.project) / "task-output"
                  if projects.get(task.project) else DATA_DIR / "task-output" / task.key)
    else:
        folder = DATA_DIR / "task-output" / task.key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def run_once(key: str, *, reason: str = "manual") -> dict[str, Any]:
    """Run one task to completion: new chat, saved transcript, dated file."""
    from . import providers, runs, store
    from .config import settings as cfg

    task = get(key)
    if not task:
        return {"ok": False, "error": f"No task {key!r}."}

    provider_key = task.provider or cfg.get("default_route") and \
        f"route:{cfg.get('default_route')}" or cfg.get("default_provider", "ollama")
    provider = providers.get(provider_key)
    if provider is None:
        return {"ok": False, "error": f"Provider {provider_key!r} is not available."}

    started = time.time()
    chat_id = store.create_chat(title=f"{task.name} — {time.strftime('%d %b %Y')}",
                                provider=provider_key, model=task.model)
    if task.project:
        from . import projects
        if projects.get(task.project):
            projects.assign(chat_id, task.project)
    store.add_message(chat_id, "user", task.prompt)

    run = runs.registry.start(
        chat_id, provider, task.model, use_tools=task.use_tools,
        approval_mode="unattended_allow" if task.trusted else "unattended_deny",
        **({"agent": task.agent} if task.agent else {}))
    try:
        if run.task:
            await run.task
    except Exception as exc:
        task.last_status = f"error: {exc}"

    display = store.transcript_for_display(chat_id)
    answer = ""
    for message in display:
        if message["role"] == "assistant":
            answer = message.get("content") or ""

    path = ""
    try:
        folder = output_folder(task)
        stamp = time.strftime("%Y-%m-%d-%H%M")
        file = folder / f"{task.key}-{stamp}.md"
        file.write_text(
            f"# {task.name}\n\n*{time.strftime('%A %d %B %Y, %H:%M')} · "
            f"{provider_key} · triggered {reason}*\n\n"
            f"## Prompt\n\n{task.prompt}\n\n## Result\n\n{answer or '(no output)'}\n",
            encoding="utf-8")
        path = str(file)
    except OSError as exc:
        task.last_status = f"ran, but could not write the file: {exc}"

    task.last_run = started
    task.runs += 1
    task.last_chat_id = chat_id
    task.last_file = path
    if not task.last_status.startswith(("error", "ran,")):
        task.last_status = f"ok ({run.stop_reason or 'done'})"
    save(task)

    return {"ok": True, "chat_id": chat_id, "file": path,
            "seconds": round(time.time() - started, 1),
            "stop_reason": run.stop_reason,
            "chars": len(answer), "status": task.last_status}


# ---------------------------------------------------------------------------
# The in-app loop
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self) -> None:
        self.loop_task: asyncio.Task | None = None
        self.last_tick: float = 0.0
        self.running: set[str] = set()

    def start(self) -> None:
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            try:
                await self.loop_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        await self.catch_up()
        while True:
            try:
                await asyncio.sleep(CHECK_EVERY)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue          # a bad task must not kill the scheduler

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Run anything whose scheduled minute has arrived."""
        now = now or datetime.now()
        fired: list[str] = []
        for key, task in tasks().items():
            if not task.enabled or key in self.running:
                continue
            due = previous_run(task.cron, now)
            if due is None:
                continue
            # Fire once per scheduled minute, and only if we have not already.
            if due.timestamp() <= task.last_run:
                continue
            if (now - due).total_seconds() > CHECK_EVERY * 4:
                continue          # too old for this tick; catch_up handles it
            fired.append(key)
            self.running.add(key)
            try:
                await run_once(key, reason="schedule")
            finally:
                self.running.discard(key)
        self.last_tick = time.time()
        return fired

    async def catch_up(self) -> list[str]:
        """On launch, run anything that was missed while AEGIS was closed.

        Once per task, not once per missed occurrence - coming back from a week
        away should not produce seven job searches.
        """
        now = datetime.now()
        fired: list[str] = []
        for key, task in tasks().items():
            if not (task.enabled and task.catch_up):
                continue
            due = previous_run(task.cron, now)
            if due is None:
                continue
            missed = due.timestamp() > task.last_run
            recent = (now - due).total_seconds() <= MAX_CATCHUP_AGE
            if missed and recent and key not in self.running:
                fired.append(key)
                self.running.add(key)
                try:
                    await run_once(key, reason="catch-up")
                finally:
                    self.running.discard(key)
        return fired

    def status(self) -> dict[str, Any]:
        return {"running": sorted(self.running),
                "alive": bool(self.loop_task and not self.loop_task.done()),
                "last_tick": self.last_tick,
                "check_every": CHECK_EVERY}


scheduler = Scheduler()


def summary() -> dict[str, Any]:
    return {
        "tasks": [t.to_dict() for t in
                  sorted(tasks().values(), key=lambda t: t.name.lower())],
        "scheduler": scheduler.status(),
        "windows": sys.platform == "win32",
        "default_output": str(DATA_DIR / "task-output"),
    }
