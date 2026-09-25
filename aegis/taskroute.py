"""Smart routing: pick the model that suits *this* request.

A route is still an ordered list you control. With `smart` on, AEGIS looks at
what the request actually is before walking that list, and moves the models
that suit it to the front:

    code       coder models first (Qwen-Coder, Devstral, DeepSeek, Kimi, GLM...)
    vision     only models that can see, when a picture is attached
    long       big-context models for long documents and transcripts
    reason     thinking models for planning, analysis, maths
    quick      small fast models for a one-line question
    creative   strong general writers for songs, stories, prompts
    chat       everything else - your order stands

Nothing here is a hard rule except what a model *cannot* do (router.plan
excludes those). Everything else is a nudge: the score only reorders, and ties
keep your order. Measured health counts too - a model that has been failing
drops, one that has been fast rises for quick questions.

The name patterns are deliberately loose. They are a prior, not a registry:
a model AEGIS has never heard of simply keeps its place in your list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CHARS_PER_TOKEN = 3.6

_CODE = re.compile(
    r"(```|\bdef \w+\(|\bfunction\b|\bclass \w+|\bimport \w+|traceback|"
    r"exception|stack ?trace|\bbug\b|\berror\b.*\bline\b|\brefactor|\bdebug|"
    r"\bcompile|\bscript\b|\bcode\b|\bcoding\b|\bpython\b|\bjavascript\b|"
    r"\btypescript\b|\bpowershell\b|\bpowercli\b|\bbash\b|\bsql\b|\bregex\b|"
    r"\bapi\b|\bjson\b|\byaml\b|\bdocker|\bgit\b|\brepo\b|\bfunction call|"
    r"\.py\b|\.js\b|\.ts\b|\.ps1\b|\.cs\b|\.java\b|\.go\b|\.rs\b|\bhtml\b|"
    r"\bcss\b|\breact\b|\bfastapi\b|\bnpm\b|\bpip install|\bbuild (me )?an? "
    r"(app|website|tool|bot|agent|script))", re.IGNORECASE)
_REASON = re.compile(
    r"(\bwhy\b|\banaly[sz]e|\banalysis|\bcompare|\bplan\b|\bstrategy|"
    r"\bstep[- ]by[- ]step|\bprove|\bcalculate|\bmath|\bequation|\bderive|"
    r"\broot cause|\btrade-?offs?|\bpros and cons|\bdecide|\bevaluate|"
    r"\barchitecture|\bdesign (a|the) system)", re.IGNORECASE)
_CREATIVE = re.compile(
    r"(\bsong|\blyrics|\bpoem|\bstory|\bverse|\bchorus|\bsuno\b|\bscript for|"
    r"\bcaption|\bslogan|\bimage prompt|\bthumbnail|\bcoup[eé]|\bsoukous|"
    r"\brap\b|\bnovel|\bcharacter|\bcreative|\bwrite (me )?a)", re.IGNORECASE)

# Model-name priors per profile. Loose on purpose.
PRIORS: dict[str, list[tuple[str, int]]] = {
    "code": [
        (r"coder|codestral|devstral|code|swe", 3),
        (r"qwen3|deepseek|kimi|glm-?4\.[5-9]|glm-?5|minimax|gpt-?5|gpt-?4\.1|"
         r"claude|sonnet|opus|gemini-?(2\.5|3)-?pro|grok", 2),
        (r"gemini|llama-?4|mistral-(large|medium)|gpt-oss", 1),
    ],
    "vision": [
        (r"vl\b|-vl|vision|pixtral|llava|qwen2\.5-vl|qwen3-vl", 3),
        (r"gemini|gpt-?4o|gpt-?4\.1|gpt-?5|claude|llama-?4|gemma-?3|"
         r"mistral-small-3|kimi-vl|glm-4\.[5-9]v", 2),
    ],
    "long": [
        (r"gemini|atria|minimax|llama-?4-scout|kimi|qwen-?long|1m|256k|200k", 3),
        (r"claude|gpt-?4\.1|gpt-?5|deepseek|glm", 1),
    ],
    "reason": [
        (r"r1\b|-r1|reason|thinking|think|qwq|o3|o4|magistral", 3),
        (r"gpt-?5|opus|gemini-?(2\.5|3)-?pro|kimi-k2|deepseek-v3|glm-?4\.[5-9]|"
         r"qwen3-235|grok-4", 2),
    ],
    "quick": [
        (r"flash|mini|nano|lite|small|haiku|instant|turbo|8b|7b|4b|3b|1b", 2),
    ],
    "creative": [
        (r"claude|sonnet|opus|gpt-?5|gpt-?4o|gemini-?(2\.5|3)|kimi|deepseek|"
         r"mistral-(large|medium)|llama-?4-maverick|grok", 2),
    ],
    "chat": [],
}

# Providers known for raw speed; quick questions go there first.
FAST_PROVIDERS = {"groq", "cerebras", "sambanova"}
LOCAL = {"ollama", "lmstudio", "llamacpp"}


@dataclass
class Profile:
    kind: str = "chat"
    images: bool = False
    tokens: int = 0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "images": self.images, "tokens": self.tokens,
                "reasons": self.reasons}


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        for call in m.get("tool_calls") or []:
            total += len(str(call.get("arguments") or ""))
        total += 800 * len(m.get("images") or [])
    return int(total / CHARS_PER_TOKEN)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def profile(messages: list[dict[str, Any]]) -> Profile:
    """Read what kind of request this is from the conversation."""
    text = _last_user_text(messages)
    tokens = estimate_tokens(messages)
    images = any(m.get("images") for m in messages if m.get("role") == "user")
    result = Profile(images=images, tokens=tokens)

    if images:
        result.kind = "vision"
        result.reasons.append("an image is attached")
    elif tokens > 24_000:
        result.kind = "long"
        result.reasons.append(f"about {tokens:,} tokens of context")
    elif _CODE.search(text):
        result.kind = "code"
        result.reasons.append("looks like code")
    elif _CREATIVE.search(text):
        result.kind = "creative"
        result.reasons.append("creative writing")
    elif _REASON.search(text):
        result.kind = "reason"
        result.reasons.append("needs reasoning")
    elif len(text) < 140 and "\n" not in text.strip():
        result.kind = "quick"
        result.reasons.append("short question")
    return result


def prior(kind: str, model: str) -> int:
    name = model.lower()
    best = 0
    for pattern, score in PRIORS.get(kind, []):
        if re.search(pattern, name):
            best = max(best, score)
    return best


def score(kind: str, provider: str, model: str) -> float:
    from . import router

    s = float(prior(kind, model))
    health = router.book.peek(provider, model)
    # A model that keeps failing drops; one that works steadily rises a bit.
    if health.consecutive_failures:
        s -= min(health.consecutive_failures, 3) * 0.75
    elif health.successes >= 3:
        s += 0.25
    if kind == "quick":
        if provider in FAST_PROVIDERS:
            s += 1.5
        tps = health.tps or 0
        if tps >= 80:
            s += 1.0
        elif tps >= 30:
            s += 0.5
    if kind in ("code", "long", "reason") and provider in LOCAL:
        s -= 1.0         # a CPU-only laptop is the last resort for heavy work
    caps = router.caps(provider, model)
    if kind == "vision" and caps.get("vision"):
        s += 2
    if kind == "long" and caps.get("ctx"):
        s += min(int(caps["ctx"]) / 200_000, 2.0)
    return s


def order(candidates: list[Any], prof: Profile) -> list[Any]:
    """Stable reorder: best fit first, your order breaks ties."""
    if prof.kind == "chat" or len(candidates) < 2:
        return list(candidates)
    ranked = sorted(enumerate(candidates),
                    key=lambda pair: (-score(prof.kind, pair[1].provider,
                                             pair[1].model), pair[0]))
    return [c for _, c in ranked]


def explain(prof: Profile, chosen: Any | None) -> str:
    if not chosen:
        return ""
    why = ", ".join(prof.reasons) or "general chat"
    return f"{prof.kind} ({why}) → {chosen.provider}/{chosen.model}"


# ---------------------------------------------------------------------------
# Picking free models worth putting in a route
# ---------------------------------------------------------------------------

QUALITY = [
    (r"gpt-?5|claude|opus|sonnet|gemini-?(2\.5|3)-?pro|kimi-k2|deepseek-(v3|r1|"
     r"chat|reasoner)|qwen3-(coder|235|480)|glm-?4\.[5-9]|glm-?5|grok-4|"
     r"llama-?4-maverick|minimax-m|devstral|gpt-oss-120", 5),
    (r"gemini-?(2\.5|3)?-?flash|qwen3|mistral-(large|medium)|llama-?3\.3-70|"
     r"llama-?4|gpt-oss|nemotron|hermes-?[34]|gemma-?3-27|mistral-small", 3),
    (r"70b|72b|32b|27b|coder|vl", 2),
]
JUNK = re.compile(r"(embed|whisper|tts|rerank|moderation|guard|audio|"
                  r"transcribe|dall-?e|flux|stable-diffusion|sdxl|image|video|"
                  r"imagen|veo|sora|speech)", re.IGNORECASE)


def quality(model_id: str) -> int:
    name = model_id.lower()
    best = 0
    for pattern, s in QUALITY:
        if re.search(pattern, name):
            best = max(best, s)
    return best


def pick_for_route(models: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Choose a useful spread of free chat models from one provider's catalogue.

    Not the first N alphabetically - the strongest ones, plus at least one
    coder, one that can see and one small fast one, so a smart route has
    something for every kind of request.
    """
    chat = [m for m in models if not JUNK.search(str(m.get("id", "")))]
    if not chat:
        return []
    chosen: list[dict[str, Any]] = []

    def add(entry: dict[str, Any] | None) -> None:
        if entry and entry not in chosen and len(chosen) < limit:
            chosen.append(entry)

    by_quality = sorted(chat, key=lambda m: (-quality(m["id"]),
                                             -(1 if m.get("tools") else 0),
                                             -(m.get("context") or 0), m["id"]))
    for m in by_quality[: max(1, limit - 3)]:
        add(m)
    add(next((m for m in by_quality if prior("code", m["id"]) >= 3), None))
    add(next((m for m in by_quality
              if m.get("vision") or prior("vision", m["id"]) >= 2), None))
    add(next((m for m in by_quality if prior("quick", m["id"]) >= 2), None))
    for m in by_quality:
        add(m)
    return chosen
