"""What AEGIS knows about you - on this PC, in three privacy tiers.

The knowledge lives as editable Markdown files in %LOCALAPPDATA%\\Aegis\\personal
(seeded from personal_bundled/ on first run; your edits are never overwritten).
Each file declares a tier:

    public     safe anywhere: who you are, how he likes to work, projects,
               interests, tech setup. The short profile in every prompt is
               built from these.
    personal   career detail, job-search rules and salary floors. Handed to
               any model when a task needs it (me__about), because a CV or a
               job search is impossible without it.
    private    family, children, health, money and admin. Local models get it
               freely. A cloud model must ask through me__private, which is a
               write-class tool - so under the default policy you see
               "share family details with this model?" and decide.

The same rule covers the life-OS skills that carry private detail
(my-profile, family-and-school, health-and-medical, money-and-benefits,
immigration, forms-and-admin): a cloud model is not given them
silently, it has to go through the same gate.

Whether the current run is local is a context variable set by the run, so the
tools and the skill loader can decide without being told every time.
"""

from __future__ import annotations

import contextvars
import re
import shutil
from pathlib import Path
from typing import Any

from .config import DATA_DIR

BUNDLED_DIR = Path(__file__).parent / "personal_bundled"
PERSONAL_DIR = DATA_DIR / "personal"
TIERS = ("public", "personal", "private")
PRIVATE_SKILLS = {"my-profile", "family-and-school", "health-and-medical",
                  "money-and-benefits", "immigration", "forms-and-admin"}
ALWAYS_IN_PROMPT = ("about", "working-style")

_local_run: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aegis_local_run", default=False)


def set_local(local: bool) -> None:
    _local_run.set(bool(local))


def is_local() -> bool:
    return _local_run.get()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def ensure() -> Path:
    PERSONAL_DIR.mkdir(parents=True, exist_ok=True)
    if BUNDLED_DIR.is_dir():
        for src in BUNDLED_DIR.glob("*.md"):
            dest = PERSONAL_DIR / src.name
            if not dest.exists():           # never overwrite your edits
                shutil.copyfile(src, dest)
    return PERSONAL_DIR


def _parse(path: Path) -> dict[str, Any]:
    from .skills import parse_frontmatter
    text = path.read_text("utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    tier = str(meta.get("tier", "private")).strip().lower()
    if tier not in TIERS:
        tier = "private"                    # unknown means the careful choice
    topics = [t.strip().lower() for t in str(meta.get("topics", "")).split(",")
              if t.strip()]
    return {"name": path.stem, "title": meta.get("title") or path.stem,
            "tier": tier, "topics": topics, "body": body.strip(),
            "path": str(path), "bytes": len(text.encode("utf-8"))}


def entries() -> list[dict[str, Any]]:
    folder = ensure()
    return [_parse(p) for p in sorted(folder.glob("*.md"))]


def get(name: str) -> dict[str, Any] | None:
    path = ensure() / f"{_safe(name)}.md"
    return _parse(path) if path.is_file() else None


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", str(name).lower())[:60].strip("-")


def save(name: str, text: str) -> dict[str, Any]:
    name = _safe(name)
    if not name:
        return {"ok": False, "error": "A file name is required."}
    if not text.lstrip().startswith("---"):
        text = f"---\ntitle: {name}\ntier: private\ntopics: {name}\n---\n{text}"
    (ensure() / f"{name}.md").write_text(text, encoding="utf-8")
    return {"ok": True, "entry": get(name)}


def delete(name: str) -> bool:
    path = ensure() / f"{_safe(name)}.md"
    if path.is_file():
        path.unlink()
        return True
    return False


def raw(name: str) -> str:
    path = ensure() / f"{_safe(name)}.md"
    return path.read_text("utf-8", errors="replace") if path.is_file() else ""


def restore_defaults() -> int:
    """Re-copy the bundled files over your copies (asked for explicitly only)."""
    ensure()
    n = 0
    for src in BUNDLED_DIR.glob("*.md"):
        shutil.copyfile(src, PERSONAL_DIR / src.name)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Finding what applies
# ---------------------------------------------------------------------------

def match(topic: str, tiers: tuple[str, ...]) -> list[dict[str, Any]]:
    words = [w for w in re.split(r"[^a-z0-9éèàç+-]+", topic.lower()) if len(w) > 2]
    scored = []
    for entry in entries():
        if entry["tier"] not in tiers:
            continue
        hay = " ".join(entry["topics"]) + " " + entry["name"] + " " + entry["title"].lower()
        score = sum(3 for w in words if w in entry["topics"]) + \
            sum(1 for w in words if w in hay)
        if not words or score:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored]


def prompt_block() -> str:
    """The always-on profile: the public 'about' and 'working-style' files."""
    parts = []
    for name in ALWAYS_IN_PROMPT:
        entry = get(name)
        if entry and entry["tier"] == "public" and filled(entry["body"]):
            parts.append(entry["body"])
    others = [e for e in entries() if e["name"] not in ALWAYS_IN_PROMPT]
    if others:
        listing = ", ".join(f"{e['name']} ({e['tier']})" for e in others)
        parts.append("More about the user is on this PC — call me__about(topic) for "
                     f"career, job-search, projects, setup, interests; "
                     f"me__private(topic, reason) for family, health, money "
                     f"(the user approves). Files: {listing}.")
    return "\n\n".join(parts)


def filled(body: str) -> bool:
    """A note still holding {{placeholders}} is an unfilled template."""
    return bool(body.strip()) and "{{" not in body


def setup_needed() -> bool:
    """True until the user has filled in at least the 'about' note."""
    entry = get("about")
    return not (entry and filled(entry["body"]))


def private_skill_notice(name: str) -> str:
    return (f"# Specialist available: {name}\nIts instructions include private "
            f"household details, so they are not sent to cloud models "
            f"automatically. If the task needs them, call "
            f"me__private(topic='{name}', reason='…') and the user will decide.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tools(local: bool | None = None) -> list[Any]:
    from . import skills
    from .tools.base import READ, WRITE, Tool

    local = is_local() if local is None else local

    async def about(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or "")
        found = match(topic, ("public", "personal"))[:3]
        if not found:
            return {"text": "Nothing on file for that. Ask the user directly."}
        return {"text": "\n\n---\n\n".join(f"## {e['title']}\n{e['body']}"
                                           for e in found)}

    async def private(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or "")
        blocks = []
        skill_name = topic.strip().lower()
        if skill_name in PRIVATE_SKILLS and (skill := skills.get(skill_name)):
            blocks.append(f"## Skill: {skill.name}\n{skill.body}")
        for entry in match(topic, TIERS)[:2]:
            blocks.append(f"## {entry['title']}\n{entry['body']}")
        if not blocks:
            return {"text": "Nothing private on file for that."}
        return {"text": "\n\n---\n\n".join(blocks)
                        + "\n\n(Private — use only for this task; do not repeat "
                          "details that are not needed.)"}

    return [
        Tool(name="me__about", raw_name="about",
             description="Look up what AEGIS knows about the user (career, CV evidence, "
                         "job-search rules and salary floors, projects, tech setup, "
                         "interests, working style). Notes containing {{placeholders}} "
                         "are unfilled templates - never treat them as facts. Use before writing "
                         "CVs, cover letters, plans or anything personal.",
             input_schema={"type": "object", "properties": {
                 "topic": {"type": "string"}}, "required": ["topic"]},
             server="me", origin="builtin", risk=READ, handler=about),
        Tool(name="me__private", raw_name="private",
             description="Private details: family and children, health and fitness, "
                         "money/benefits/admin, and the private life-OS skills "
                         "(my-profile, family-and-school, health-and-medical, "
                         "money-and-benefits, immigration, forms-and-admin). "
                         + ("" if local else "The user is asked before anything is "
                                             "shared with a cloud model — say why "
                                             "you need it in 'reason'."),
             input_schema={"type": "object", "properties": {
                 "topic": {"type": "string"},
                 "reason": {"type": "string"}}, "required": ["topic", "reason"]},
             server="me", origin="builtin", risk=READ if local else WRITE,
             handler=private),
    ]


def summary() -> dict[str, Any]:
    return {"folder": str(ensure()),
            "entries": [{k: v for k, v in e.items() if k != "body"}
                        for e in entries()],
            "private_skills": sorted(PRIVATE_SKILLS), "tiers": list(TIERS),
            "setup_needed": setup_needed()}
