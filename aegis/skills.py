"""Skills: folders of instructions the agent can pull in when they apply.

A skill is a directory containing SKILL.md with YAML frontmatter:

    ---
    name: vmware-support
    description: >
      When to use this skill...
    ---
    # the body: the actual instructions

Three locations, in priority order:

    pending/   skills the agent wrote itself, NOT loadable until approved
    user/      your own skills, and approved self-written ones
    bundled/   the pack that ships with AEGIS

Only the *descriptions* go into the system prompt - twelve skills' worth of full
text would swamp the context. The agent reads the index, decides what applies,
and calls skill_load to pull in the body it needs. That is the same routing you
already do by hand, made explicit.

Self-authoring is gated. A skill is a standing instruction that will shape every
future reply, so a skill the agent writes lands in pending/ and does nothing at
all until you read it and approve it. The agent cannot approve its own skill:
the tool that writes them has no path to the approve function.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ensure_dirs

BUNDLED_DIR = Path(__file__).parent / "skills_bundled"
USER_DIR = DATA_DIR / "skills"
PENDING_DIR = DATA_DIR / "skills_pending"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
MAX_SKILL_BYTES = 96 * 1024


@dataclass
class Skill:
    name: str
    description: str
    body: str
    origin: str               # bundled | user | pending
    path: str
    bytes: int = 0
    approved: bool = True

    def to_dict(self, include_body: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d["loadable"] = self.approved
        if not include_body:
            d["body"] = ""
            d["preview"] = self.body[:400]
        return d


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal YAML frontmatter reader: scalars and '>' folded blocks only.

    Deliberately not a YAML parser. Skill frontmatter is two or three string
    fields; pulling in a YAML dependency to read them would be the wrong trade,
    and a real parser would happily execute tags we do not want.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")

    meta: dict[str, str] = {}
    key: str | None = None
    folded: list[str] = []

    def flush() -> None:
        nonlocal key, folded
        if key is not None:
            meta[key] = " ".join(p.strip() for p in folded if p.strip()).strip()
        key, folded = None, []

    for line in header.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match and not line.startswith((" ", "\t")):
            flush()
            field, value = match.group(1).lower(), match.group(2).strip()
            if value in (">", "|", ">-", "|-"):
                key = field
            else:
                meta[field] = value.strip("'\"")
        elif key is not None:
            folded.append(line)
    flush()
    return meta, body


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_skill(folder: Path, origin: str) -> Skill | None:
    path = folder / "SKILL.md"
    if not path.is_file():
        return None
    try:
        raw = path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(raw)
    name = (meta.get("name") or folder.name).strip().lower()
    if not NAME_RE.match(name):
        name = re.sub(r"[^a-z0-9-]", "-", folder.name.lower())[:48] or "skill"
    return Skill(
        name=name,
        description=(meta.get("description") or "").strip(),
        body=body.strip(),
        origin=origin,
        path=str(path),
        bytes=len(raw.encode("utf-8")),
        approved=(origin != "pending"),
    )


def registry() -> dict[str, Skill]:
    """Every skill, with user copies overriding bundled ones of the same name."""
    ensure_dirs()
    USER_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    found: dict[str, Skill] = {}
    for directory, origin in ((BUNDLED_DIR, "bundled"), (USER_DIR, "user")):
        if not directory.is_dir():
            continue
        for folder in sorted(directory.iterdir()):
            if folder.is_dir() and (skill := _read_skill(folder, origin)):
                found[skill.name] = skill

    # Pending skills are listed but never override a live one.
    for folder in sorted(PENDING_DIR.iterdir()) if PENDING_DIR.is_dir() else []:
        if folder.is_dir() and (skill := _read_skill(folder, "pending")):
            found.setdefault(f"pending:{skill.name}", skill)

    return found


def loadable() -> list[Skill]:
    return [s for s in registry().values() if s.approved]


def pending() -> list[Skill]:
    return [s for s in registry().values() if not s.approved]


def get(name: str) -> Skill | None:
    skill = registry().get(name.strip().lower())
    return skill if (skill and skill.approved) else None


def index_text() -> str:
    """The router block that goes into the system prompt."""
    skills = loadable()
    if not skills:
        return ""
    lines = ["You have skills available - folders of instructions for particular "
             "kinds of work. Read this index, and when one clearly applies call "
             "skill_load to pull in its full instructions before answering. "
             "Load more than one if the task spans them.", ""]
    for skill in sorted(skills, key=lambda s: s.name):
        desc = " ".join(skill.description.split())[:400]
        lines.append(f"- **{skill.name}** — {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-authoring, behind the approval gate
# ---------------------------------------------------------------------------

def draft(name: str, description: str, body: str) -> dict[str, Any]:
    """Write a skill into pending/. It is inert until a human approves it."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        return {"ok": False,
                "error": "Name must be lowercase letters, digits and hyphens, "
                         "2-49 characters."}
    if not description.strip() or not body.strip():
        return {"ok": False, "error": "Both a description and a body are required."}

    content = (f"---\nname: {name}\ndescription: >\n  "
               + "\n  ".join(" ".join(description.split())[:1200].splitlines())
               + f"\n---\n\n{body.strip()}\n")
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        return {"ok": False, "error": f"Skill exceeds {MAX_SKILL_BYTES // 1024} KB."}

    ensure_dirs()
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    folder = PENDING_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(content, "utf-8")
    (folder / ".drafted").write_text(str(time.time()), "utf-8")

    existing = name in {s.name for s in loadable()}
    return {"ok": True, "name": name, "status": "pending_approval",
            "replaces_existing": existing,
            "message": (f"Skill '{name}' written to the pending folder. It will "
                        f"NOT be used until the user approves it on the Skills "
                        f"tab. Tell them it is waiting.")}


def approve(name: str) -> dict[str, Any]:
    """Promote a pending skill into the live user folder. Human-only path."""
    folder = PENDING_DIR / name
    if not (folder / "SKILL.md").is_file():
        return {"ok": False, "error": f"No pending skill called {name!r}."}
    USER_DIR.mkdir(parents=True, exist_ok=True)
    target = USER_DIR / name
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(folder), str(target))
    return {"ok": True, "name": name, "path": str(target)}


def reject(name: str) -> dict[str, Any]:
    folder = PENDING_DIR / name
    if not folder.is_dir():
        return {"ok": False, "error": f"No pending skill called {name!r}."}
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True, "name": name}


def delete_user_skill(name: str) -> dict[str, Any]:
    """Remove a user skill. Bundled skills cannot be deleted, only overridden."""
    folder = USER_DIR / name
    if not folder.is_dir():
        return {"ok": False,
                "error": f"{name!r} is not a user skill (bundled skills stay put; "
                         f"put a skill of the same name in your skills folder to "
                         f"override one)."}
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tools() -> list[Any]:
    import asyncio

    from .tools.base import READ, WRITE, Tool

    def wrap(fn):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await asyncio.to_thread(fn, **args)
            except Exception as exc:
                return {"text": f"{type(exc).__name__}: {exc}", "error": True}
        return handler

    def _load(name: str = "") -> dict[str, Any]:
        from . import personal
        from .config import settings as _settings
        if name.strip().lower() in personal.PRIVATE_SKILLS and \
                not personal.is_local() and \
                _settings.get("profile_sharing", "lite") != "full":
            return {"text": f"'{name}' holds private household details and is "
                            f"not sent to cloud models automatically. Call "
                            f"me__private(topic='{name}', reason='why you need "
                            f"it') and the user will decide.", "error": True}
        skill = get(name)
        if not skill:
            names = ", ".join(sorted(s.name for s in loadable()))
            return {"text": f"No skill called {name!r}. Available: {names}",
                    "error": True}
        return {"text": f"# Skill: {skill.name}\n\n{skill.body}"}

    def _list() -> dict[str, Any]:
        index = index_text()
        return {"text": index or "No skills are installed."}

    def _write(name: str = "", description: str = "", body: str = "") -> dict[str, Any]:
        result = draft(name, description, body)
        if not result.get("ok"):
            return {"text": result["error"], "error": True}
        return {"text": result["message"]}

    def tool(n, desc, schema, risk, handler) -> Tool:
        return Tool(name=f"skill__{n}", raw_name=n, description=desc,
                    input_schema=schema, server="skills", origin="builtin",
                    risk=risk, handler=handler)

    return [
        tool("load",
             "Load a skill's full instructions. Do this before working on "
             "anything its description covers, and load several if the task "
             "spans them.",
             {"type": "object",
              "properties": {"name": {"type": "string"}}, "required": ["name"]},
             READ, wrap(_load)),

        tool("list", "List every available skill with its description.",
             {"type": "object", "properties": {}}, READ, wrap(_list)),

        tool("write",
             "Write a new skill for a task you expect to repeat. It is saved as "
             "a DRAFT and has no effect until the user approves it - say so "
             "rather than implying it is now in use. Write the body as direct "
             "instructions to a future assistant.",
             {"type": "object",
              "properties": {
                  "name": {"type": "string",
                           "description": "lowercase-with-hyphens"},
                  "description": {"type": "string",
                                  "description": "When this skill should be used"},
                  "body": {"type": "string",
                           "description": "The instructions, in Markdown"}},
              "required": ["name", "description", "body"]},
             WRITE, wrap(_write)),
    ]


def summary() -> dict[str, Any]:
    reg = registry()
    live = [s for s in reg.values() if s.approved]
    return {
        "skills": [s.to_dict() for s in sorted(live, key=lambda s: s.name)],
        "pending": [s.to_dict(include_body=True)
                    for s in reg.values() if not s.approved],
        "counts": {"total": len(live),
                   "bundled": sum(1 for s in live if s.origin == "bundled"),
                   "user": sum(1 for s in live if s.origin == "user"),
                   "pending": sum(1 for s in reg.values() if not s.approved)},
        "user_dir": str(USER_DIR),
        "pending_dir": str(PENDING_DIR),
    }
