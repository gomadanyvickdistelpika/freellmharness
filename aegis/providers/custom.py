"""Bring your own OpenAI-compatible provider.

OpenRouter, Groq, Together, DeepInfra, Cerebras, a self-hosted 9Router, a
LiteLLM gateway at work - they all speak /v1/chat/completions. So rather than
hardcode a list that rots, AEGIS lets you add any of them with three fields:

    name        what you want to call it
    base URL    ending in /v1 (or wherever that endpoint lives)
    API key     stored in the encrypted vault, never in settings.json

The presets below are starting points, not gospel. Base URLs move and free
tiers change constantly, so every field stays editable and AEGIS never asserts
what a provider's limits are - it asks the provider.

Free models are detected live from each provider's own /models response
(price of zero, or OpenRouter's ':free' suffix) rather than from a table here
that would be wrong within a month.
"""

from __future__ import annotations

from typing import Any

import httpx

from .. import vault
from ..config import settings
from .openai_compat import OpenAICompatProvider

# Starting points. Every one of these is editable in the UI, and AEGIS does not
# claim to know their current free allowances - it reads /models at runtime.
PRESETS: list[dict[str, str]] = [
    {"key": "freellmapi", "label": "FreeLLMAPI (local aggregator)",
     "base_url": "http://localhost:3001/v1",
     "free_tier": "yes",
     "note": "An open-source gateway you run yourself "
             "(github.com/tashfeenahmed/freellmapi). You paste your free keys "
             "for Google, Groq, Cerebras, Mistral, Cohere, NVIDIA and the rest "
             "into it once, and it hands AEGIS a single endpoint and one token "
             "that fronts all of them. Start it first - if nothing is listening "
             "on port 3001 this provider simply reports as unreachable. It does "
             "its own rotation inside; AEGIS still treats it as one candidate "
             "and falls through to the next provider when the whole gateway "
             "is spent."},
    {"key": "openrouter", "label": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1",
     "note": "Aggregator across many providers. Models ending ':free' cost nothing."},
    {"key": "groq", "label": "Groq",
     "base_url": "https://api.groq.com/openai/v1",
     "free_tier": "yes",
     "note": "Very fast inference on open models. Free plan with per-minute "
             "and per-day limits - quick questions get routed here first."},
    {"key": "together", "label": "Together AI",
     "base_url": "https://api.together.xyz/v1",
     "note": "Broad open-model catalogue."},
    {"key": "deepinfra", "label": "DeepInfra",
     "base_url": "https://api.deepinfra.com/v1/openai",
     "note": "Open models, per-token pricing."},
    {"key": "cerebras", "label": "Cerebras",
     "base_url": "https://api.cerebras.ai/v1",
     "free_tier": "yes",
     "note": "Very high tokens/sec on a small catalogue, free daily tokens."},
    {"key": "mistral", "label": "Mistral",
     "base_url": "https://api.mistral.ai/v1",
     "note": "Mistral's own endpoint."},
    {"key": "xkiro", "label": "xKiro",
     "base_url": "https://api.xkiro.com/v1",
     "note": "Gateway over ~16 vendors. Model ids are vendor/model, e.g. "
             "openai/gpt-5.6-sol. It does publish /v1/models, but the catalogue "
             "carries no per-model pricing - on xKiro 'free' is a daily token "
             "allowance on your plan, not a property of the model. So AEGIS "
             "treats them as paid until you mark the ones you want free."},
    {"key": "atria", "label": "Atria (Dawn Preview)",
     "base_url": "https://api.atria-asi.ai/v1",
     "models": "Atria-Dawn-Preview",
     "has_models": "no",
     "note": "256K context, built for long agent runs. Sign in with Google at "
             "api.atria-asi.ai/console to get a key. Model id is case-sensitive. "
             "It has no /models endpoint, so the model list is fixed here. "
             "60 requests/minute, and it sends Retry-After when you hit that."},
    {"key": "ninerouter", "label": "9Router (local, free)",
     "base_url": "http://localhost:20128/v1",
     "free_tier": "yes",
     "note": "Local free router over 40+ providers, including Kiro (kr/...), "
             "Gemini CLI, Qwen and iFlow. Install once with "
             "'npm install -g 9router', run '9router', connect providers in "
             "its dashboard at localhost:20128 and copy the API key it shows. "
             "Model ids look like kr/claude-sonnet-4.5 or your own combo names. "
             "AEGIS still falls through to your other providers when 9Router "
             "itself runs dry."},
    {"key": "pollinations", "label": "Pollinations (text + images + voice + video)",
     "base_url": "https://gen.pollinations.ai/v1",
     "free_tier": "yes",
     "note": "One key for chat models AND media: images, speech and video in "
             "the Studio. Get an sk_ key at enter.pollinations.ai. It runs on "
             "'pollen' credits with a free daily grant - untick 'free plan' if "
             "you top up and want AEGIS to treat it as paid."},
    {"key": "gemini", "label": "Google AI Studio (Gemini)",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "free_tier": "yes",
     "note": "Gemini's OpenAI-compatible endpoint. The free tier is generous "
             "for flash models and sees images. Key from aistudio.google.com."},
    {"key": "hfrouter", "label": "Hugging Face router",
     "base_url": "https://router.huggingface.co/v1",
     "free_tier": "yes",
     "note": "Your HF token, many open models across inference providers. "
             "Small free monthly credit - AEGIS moves on when it is spent."},
    {"key": "github", "label": "GitHub Models",
     "base_url": "https://models.github.ai/inference",
     "free_tier": "yes",
     "has_models": "no",
     "models": "openai/gpt-4.1, openai/gpt-4.1-mini, openai/gpt-4o, "
               "deepseek/DeepSeek-V3-0324, meta/Llama-4-Maverick-17B-128E-Instruct-FP8",
     "note": "Free, rate-limited, with a GitHub token (models:read). No "
             "/models on this endpoint, so a starter list is filled in."},
    {"key": "nvidia", "label": "NVIDIA NIM",
     "base_url": "https://integrate.api.nvidia.com/v1",
     "free_tier": "yes",
     "note": "Free developer credits on build.nvidia.com; strong open models "
             "(DeepSeek, Qwen, Llama, Kimi)."},
    {"key": "custom", "label": "Something else",
     "base_url": "",
     "note": "Any OpenAI-compatible endpoint: a LiteLLM gateway, a work proxy, "
             "anything that serves /chat/completions."},
]


def stored() -> dict[str, dict[str, Any]]:
    return dict(settings.get("custom_providers") or {})


def save(key: str, label: str, base_url: str, *,
         note: str = "", extra_headers: dict[str, str] | None = None,
         models: list[str] | None = None,
         has_models_endpoint: bool = True,
         free_tier: bool = False) -> dict[str, Any]:
    """Store a provider.

    free_tier says "this whole account is on a free plan" - which is how xKiro,
    9Router and an Atria preview key actually work: the allowance belongs to the
    plan, not to any individual model. Setting it means every model here counts
    as free, so a free-only route can use them all without tagging each one by
    hand. It is your assertion about your own account, not a guess by AEGIS.
    """
    key = "".join(ch for ch in key.strip().lower()
                  if ch.isalnum() or ch in "-_")[:32]
    if not key:
        return {"ok": False, "error": "A short name is required."}
    if not base_url.strip().startswith(("http://", "https://")):
        return {"ok": False, "error": "The base URL must start with http:// or https://"}

    models = [m.strip() for m in (models or []) if m.strip()]
    if not has_models_endpoint and not models:
        return {"ok": False,
                "error": "This provider has no /models endpoint, so list at "
                         "least one model id by hand."}

    providers = stored()
    providers[key] = {
        "label": label.strip() or key,
        "base_url": base_url.strip().rstrip("/"),
        "note": note.strip(),
        "headers": {str(k): str(v) for k, v in (extra_headers or {}).items()},
        "models": models,
        "has_models_endpoint": bool(has_models_endpoint),
        "free_tier": bool(free_tier),
    }
    settings.set("custom_providers", providers)
    return {"ok": True, "key": key}


def is_free_tier(provider_key: str) -> bool:
    return bool((stored().get(provider_key) or {}).get("free_tier"))


def remove(key: str) -> bool:
    providers = stored()
    existed = key in providers
    providers.pop(key, None)
    settings.set("custom_providers", providers)
    vault.delete(key_name(key))
    vault.delete(extra_keys_name(key))
    return existed


def key_name(provider_key: str) -> str:
    return f"custom_key:{provider_key}"


def set_api_key(provider_key: str, api_key: str) -> None:
    if api_key:
        vault.put(key_name(provider_key), api_key)
    else:
        vault.delete(key_name(provider_key))


def extra_keys_name(provider_key: str) -> str:
    return f"custom_keys:{provider_key}"


def all_keys(provider_key: str) -> list[str]:
    """The main key plus any extra keys, for FreeLLMAPI-style rotation."""
    keys = []
    if main := vault.get(key_name(provider_key)):
        keys.append(main)
    for line in (vault.get(extra_keys_name(provider_key)) or "").splitlines():
        line = line.strip()
        if line and line not in keys:
            keys.append(line)
    return keys


def set_extra_keys(provider_key: str, text: str) -> int:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if lines:
        vault.put(extra_keys_name(provider_key), "\n".join(lines))
    else:
        vault.delete(extra_keys_name(provider_key))
    return len(lines)


def build() -> list[OpenAICompatProvider]:
    """One Provider per configured custom endpoint."""
    out: list[OpenAICompatProvider] = []
    for key, spec in stored().items():
        base = spec.get("base_url") or ""
        if not base:
            continue
        provider = OpenAICompatProvider(
            key=key,
            label=spec.get("label", key),
            base_url=base,
            blurb=spec.get("note", "") or "OpenAI-compatible endpoint",
            needs="api_key",
            auth=(lambda k=key: vault.get(key_name(k))),
            extra_headers=dict(spec.get("headers") or {}),
            default_models=list(spec.get("models") or []),
            has_models_endpoint=bool(spec.get("has_models_endpoint", True)),
        )
        provider._keys = (lambda k=key: all_keys(k))
        out.append(provider)
    return out


# ---------------------------------------------------------------------------
# Live model catalogue, including which ones are free
# ---------------------------------------------------------------------------

def _price_of(entry: dict[str, Any]) -> float | None:
    """Prompt price per token, if the provider reports one."""
    pricing = entry.get("pricing")
    if isinstance(pricing, dict):
        for field in ("prompt", "input", "completion"):
            value = pricing.get(field)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _capabilities(entry: dict[str, Any]) -> dict[str, Any]:
    """What a catalogue says a model can do, where it says anything at all.

    OpenRouter publishes supported_parameters and input_modalities; others
    publish capabilities blocks or nothing. Unknown stays None - never guessed.
    """
    out: dict[str, Any] = {"tools": None, "vision": None}
    params = entry.get("supported_parameters")
    if isinstance(params, list):
        out["tools"] = "tools" in params or "tool_choice" in params
    arch = entry.get("architecture") or {}
    modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
    if isinstance(modalities, list):
        out["vision"] = "image" in modalities
    elif isinstance(arch, dict) and isinstance(arch.get("modality"), str):
        out["vision"] = "image" in arch["modality"].split("->")[0]
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, dict):
        if "function_calling" in capabilities or "tools" in capabilities:
            out["tools"] = bool(capabilities.get("function_calling")
                                or capabilities.get("tools"))
        if "vision" in capabilities:
            out["vision"] = bool(capabilities.get("vision"))
    return out


async def list_models(provider_key: str) -> dict[str, Any]:
    """Ask the provider what it has, and which of it is free.

    Deliberately reads the live endpoint rather than consulting a built-in
    table: free tiers and model names change faster than any shipped list.
    """
    spec = stored().get(provider_key)
    if not spec:
        return {"ok": False, "error": f"No provider called {provider_key!r}."}

    if not spec.get("has_models_endpoint", True):
        # Nothing to query. Report the hand-entered list rather than failing,
        # and say plainly that free/paid is unknown for it.
        models = [{"id": m, "free": f"{provider_key}::{m}" in
                   set(settings.get("free_models") or []),
                   "price_per_token": None, "context": None, "name": m}
                  for m in (spec.get("models") or [])]
        return {"ok": True, "models": models, "count": len(models),
                "free": sum(1 for m in models if m["free"]), "priced": False,
                "note": ("This provider publishes no /models endpoint, so this "
                         "is the list you entered. AEGIS cannot tell whether "
                         "these cost money - tag them yourself on the route.")}

    headers = {"Content-Type": "application/json", **(spec.get("headers") or {})}
    if token := vault.get(key_name(provider_key)):
        headers["Authorization"] = f"Bearer {token}"

    url = f"{spec['base_url'].rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=headers, timeout=20.0)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if response.status_code >= 400:
        return {"ok": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "error": "The provider did not return JSON."}

    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return {"ok": False, "error": "Unexpected /models response shape."}

    models: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id") or entry.get("name")
        if not model_id:
            continue
        price = _price_of(entry)
        free = model_id.endswith(":free") or (price is not None and price == 0.0)
        models.append({
            "id": model_id,
            "free": free,
            "price_per_token": price,
            "context": entry.get("context_length")
                       or (entry.get("top_provider") or {}).get("context_length"),
            "name": entry.get("name") or model_id,
            **_capabilities(entry),
        })

    priced = any(m["price_per_token"] is not None for m in models)
    try:
        from .. import router
        router.learn_caps_bulk(provider_key, [
            {"id": m["id"], "tools": m.get("tools"), "vision": m.get("vision"),
             "ctx": m.get("context")} for m in models])
    except Exception:
        pass        # capabilities are a bonus, never a reason to fail a listing
    if spec.get("free_tier"):
        # You have told us this whole account is on a free plan, so stop
        # second-guessing it per model.
        for entry in models:
            entry["free"] = True
        models.sort(key=lambda m: m["id"])
        return {"ok": True, "models": models, "count": len(models),
                "free": len(models), "priced": priced,
                "note": "Marked free because this provider is set to free-tier."}

    # Tags the user set by hand must survive a refresh. Only a provider that
    # actually publishes prices gets to overwrite them; one that publishes none
    # (xKiro, where 'free' is a plan allowance rather than a model property)
    # would otherwise wipe every manual tag on every listing.
    tags = set(settings.get("free_models") or [])
    if priced:
        tags = {t for t in tags if not t.startswith(f"{provider_key}::")}
        tags |= {f"{provider_key}::{m['id']}" for m in models if m["free"]}
        settings.set("free_models", sorted(tags))
    else:
        for entry in models:
            entry["free"] = f"{provider_key}::{entry['id']}" in tags

    models.sort(key=lambda m: (not m["free"], m["id"]))

    result = {"ok": True, "models": models, "count": len(models),
              "free": sum(1 for m in models if m["free"]), "priced": priced}
    if not priced:
        result["note"] = ("This catalogue publishes no per-model pricing, so "
                          "AEGIS cannot tell free from paid. Mark the ones your "
                          "plan covers and they become usable on a free route.")
    return result


def summary() -> dict[str, Any]:
    providers = []
    for key, spec in stored().items():
        providers.append({
            "key": key,
            "label": spec.get("label", key),
            "base_url": spec.get("base_url", ""),
            "note": spec.get("note", ""),
            "has_key": vault.has(key_name(key)),
            "masked": vault.masked(key_name(key)),
            "key_count": len(all_keys(key)),
            "models": list(spec.get("models") or []),
            "has_models_endpoint": bool(spec.get("has_models_endpoint", True)),
            "free_tier": bool(spec.get("free_tier")),
        })
    return {"providers": sorted(providers, key=lambda p: p["label"]),
            "presets": PRESETS}
