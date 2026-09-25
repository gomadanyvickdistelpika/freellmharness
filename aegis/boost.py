"""Boost: make a free model answer like a much stronger one.

Three cheap techniques that reliably lift small and free models, used only for
the requests that deserve it (Settings -> Boost: auto / always / off):

  1. Work in steps.   A demanding request gets a short "plan, do, verify"
                      instruction and the plan__update tool, so the model
                      writes its plan down before acting.
  2. Second opinion.  When the answer is finished, a *different* model from
                      the same route reviews it against the question - wrong
                      facts, broken code, missed requirements, ignored
                      constraints. A clean answer gets "LGTM" and nothing else
                      happens.
  3. Revise.          If the reviewer found real problems, the answering model
                      rewrites the complete answer with that feedback. The
                      first draft is kept, folded away, so you can compare.

A review costs one extra request; a revision one more. Free and paid rules
still apply - the reviewer is picked from the same route, so a free route is
reviewed by a free model.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from .config import settings

HARD_KINDS = {"code", "reason", "long"}
HARD_SPECIALISTS = {"coding", "job-search", "vmware-support", "research",
                    "building-and-projects", "planner"}
MIN_ANSWER_CHARS = 180
MAX_REVIEW_INPUT = 24000

PLAN_NOTE = ("This is a demanding request. Work like a senior professional: "
             "first write a short plan (use plan__update if you have it), then "
             "do the work step by step using tools where they give real "
             "information, check your result against every requirement in the "
             "request, and only then give the final answer.")

REVIEW_SYSTEM = ("You are a strict senior reviewer. You check an assistant's "
                 "answer for real problems only: factual errors, wrong or "
                 "unsafe commands, code that would not run, requirements or "
                 "constraints from the question that were ignored, invented "
                 "sources/KBs/numbers, and important missing steps. Style "
                 "preferences are not problems.")


def mode(override: str = "") -> str:
    value = (override or settings.get("boost_mode") or "auto").lower()
    return value if value in ("auto", "always", "off") else "auto"


def applies(profile_kind: str, specialists: list[str], text: str,
            override: str = "") -> bool:
    chosen = mode(override)
    if chosen == "off":
        return False
    if chosen == "always":
        return True
    if len(text.strip()) < 20:
        return False
    return profile_kind in HARD_KINDS or bool(set(specialists) & HARD_SPECIALISTS)


def critic_for(provider: Any, model: str) -> list[tuple[Any, str, str]]:
    """Reviewers to try, best first: a different model from the same route."""
    from . import router
    from .providers import registry

    route = getattr(provider, "route", None)
    if route is None:
        return [(provider, model, f"{getattr(provider, 'key', '')}/{model}")]
    used = getattr(provider, "last_used", None)
    plan = router.plan(route)
    available = registry()
    ranked = []
    for cand in plan.usable:
        p = available.get(cand.provider)
        if p is None:
            continue
        same_model = used is not None and cand.model == used.model and \
            cand.provider == used.provider
        same_provider = used is not None and cand.provider == used.provider
        ranked.append((2 if same_model else 1 if same_provider else 0,
                       (p, cand.model, f"{cand.provider}/{cand.model}")))
    ranked.sort(key=lambda x: x[0])
    return [r for _, r in ranked][:3]


async def _complete(provider: Any, model: str, messages: list[dict[str, Any]]) -> str:
    out: list[str] = []
    async for event in provider.chat(messages, model):
        if event.get("type") == "delta":
            out.append(event.get("text", ""))
        elif event.get("type") == "error":
            return ""
    return "".join(out).strip()


async def review(provider: Any, model: str, question: str,
                 answer: str) -> tuple[str, str]:
    """(verdict_text, reviewer_label). verdict '' means no reviewer answered."""
    prompt = (f"QUESTION:\n{question[-6000:]}\n\nANSWER TO REVIEW:\n"
              f"{answer[:MAX_REVIEW_INPUT]}\n\nIf the answer is correct and "
              f"complete, reply with exactly: LGTM\nOtherwise list at most 6 "
              f"concrete problems as short bullets, most important first. Do "
              f"not rewrite the answer.")
    for critic, critic_model, label in critic_for(provider, model):
        verdict = await _complete(critic, critic_model,
                                  [{"role": "system", "content": REVIEW_SYSTEM},
                                   {"role": "user", "content": prompt}])
        if verdict:
            return verdict, label
    return "", ""


def is_clean(verdict: str) -> bool:
    head = verdict.strip().strip("*`").upper()
    return head.startswith("LGTM") or bool(re.match(r"^(NO (REAL )?PROBLEMS|LOOKS GOOD)", head))


async def revise(provider: Any, model: str, messages: list[dict[str, Any]],
                 draft: str, verdict: str) -> AsyncIterator[dict[str, Any]]:
    transcript = [*messages,
                  {"role": "assistant", "content": draft},
                  {"role": "user", "content":
                   "A reviewer checked your answer and found these problems:\n"
                   f"{verdict}\n\nWrite the complete, improved final answer that "
                   "fixes them. Keep everything that was right. Output only the "
                   "answer itself - no mention of the review."}]
    async for event in provider.chat(transcript, model):
        if event.get("type") in ("delta", "notice", "usage", "error",
                                 "route_pick"):
            if event.get("type") == "route_pick":
                continue
            yield event


def plan_tools() -> list[Any]:
    """plan__update - a visible checklist, like Claude's todo list."""
    from .tools.base import READ, Tool

    marks = {"done": "[x]", "completed": "[x]", "in_progress": "[~]",
             "doing": "[~]", "pending": "[ ]", "todo": "[ ]"}

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        items = args.get("items") or []
        lines = []
        for item in items[:20]:
            if isinstance(item, str):
                item = {"step": item}
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "pending").lower()
            step = str(item.get("step") or item.get("text") or "").strip()
            if step:
                lines.append(f"- {marks.get(status, '[ ]')} {step}")
        if not lines:
            return {"text": "Give items as a list of {step, status}.", "error": True}
        return {"text": "Plan:\n" + "\n".join(lines)}

    return [Tool(
        name="plan__update", raw_name="update",
        description=("Write or update your step-by-step plan for this task, as "
                     "a checklist the user can see. Call it at the start of a "
                     "multi-step task and again as steps complete."),
        input_schema={"type": "object", "properties": {"items": {
            "type": "array", "items": {"type": "object", "properties": {
                "step": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["pending", "in_progress", "done"]}},
                "required": ["step"]}}},
            "required": ["items"]},
        server="plan", origin="builtin", risk=READ, handler=handler)]
