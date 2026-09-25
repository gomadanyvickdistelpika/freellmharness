"""Zero-setup free routing: paste keys, get a working smart route.

v1 had the "Build my free route" button. v2 makes that the default path:

  * quick_setup()  takes a handful of API keys for the free presets, saves the
                   providers, builds the Auto route and makes it the default -
                   one form, no route editing.
  * build()        asks every provider what it offers today, keeps the free
                   ones, and picks a useful *spread* from each (strongest,
                   a coder, one that can see, a fast one) rather than the
                   first few alphabetically.
  * refresh_if_stale()  rebuilds the Auto route in the background at start-up
                   when it is more than a day old - free catalogues change
                   weekly - but only while you have not hand-edited it.
"""

from __future__ import annotations

import time
from typing import Any

from . import router, taskroute
from .config import settings

AUTO_KEY = "auto"
STALE_AFTER = 20 * 60 * 60


async def build(key: str = AUTO_KEY, per_provider: int = 6, *,
                label: str = "", smart: bool = True) -> dict[str, Any]:
    from . import providers
    from .providers import custom as custom_providers

    per_provider = max(1, min(int(per_provider or 6), 12))
    cloud: list[router.Candidate] = []
    local: list[router.Candidate] = []
    notes: list[str] = []

    for provider_key, spec in custom_providers.stored().items():
        name = spec.get("label", provider_key)
        free_tier = bool(spec.get("free_tier"))
        listing = await custom_providers.list_models(provider_key)
        if not listing.get("ok"):
            notes.append(f"{name}: {listing.get('error', 'could not list models')}")
            continue
        picks = [m for m in listing["models"] if free_tier or m.get("free")]
        if not picks:
            notes.append(f"{name}: no free models found"
                         + ("" if free_tier else " — mark it free-tier if your "
                                                 "plan covers it"))
            continue
        chosen = taskroute.pick_for_route(picks, per_provider)
        for entry in chosen:
            cloud.append(router.Candidate(provider=provider_key,
                                          model=entry["id"],
                                          note=f"free on {name}"))
        if not chosen:
            notes.append(f"{name}: only non-chat models (images, embeddings…)")

    # Interleave providers so one provider's outage does not sit on top of the
    # list five times: best of each first, then second-best of each, and so on.
    cloud = _interleave(cloud)

    for engine in ("ollama", "lmstudio"):
        provider = providers.get(engine)
        if provider is None:
            continue
        try:
            models = await provider.models()
        except Exception:
            models = []
        models = [m for m in models if not taskroute.JUNK.search(m)]
        for model in models[:min(per_provider, 3)]:
            local.append(router.Candidate(provider=engine, model=model,
                                          note="local — never runs out"))

    candidates = [*cloud, *local]
    if not candidates:
        return {"ok": False,
                "error": "Nothing free to add yet. Add a key on the Providers "
                         "tab (Quick setup), mark a provider free-tier if your "
                         "plan covers it, or start Ollama.",
                "notes": notes}

    existing = router.get_route(key)
    router.save_route(router.Route(
        key=key,
        label=label or (existing.label if existing else "Auto (free, smart)"),
        description=("Built automatically from every free provider, refreshed "
                     "daily. Edit it and AEGIS stops refreshing it."),
        allow_paid=False,
        smart=smart,
        then_route=existing.then_route if existing else "",
        candidates=candidates))
    if key == AUTO_KEY:
        settings.update({"auto_route_built": time.time(),
                         "auto_route_managed": True})

    return {"ok": True, "key": key, "added": len(candidates),
            "cloud": len(cloud), "local": len(local), "notes": notes,
            "route": router.get_route(key).to_dict()}


def _interleave(cands: list[router.Candidate]) -> list[router.Candidate]:
    buckets: dict[str, list[router.Candidate]] = {}
    for c in cands:
        buckets.setdefault(c.provider, []).append(c)
    out: list[router.Candidate] = []
    while any(buckets.values()):
        for provider in list(buckets):
            if buckets[provider]:
                out.append(buckets[provider].pop(0))
    return out


async def quick_setup(keys: dict[str, str], *, make_default: bool = True
                      ) -> dict[str, Any]:
    """Save several free providers from their presets at once, then build Auto."""
    from .providers import custom as custom_providers

    presets = {p["key"]: p for p in custom_providers.PRESETS}
    saved, skipped = [], []
    for preset_key, api_key in (keys or {}).items():
        api_key = str(api_key or "").strip()
        preset = presets.get(preset_key)
        if not preset or not api_key:
            continue
        if not preset.get("base_url"):
            skipped.append(f"{preset['label']}: needs a base URL")
            continue
        models = [m.strip() for m in str(preset.get("models", "")).split(",")
                  if m.strip()]
        result = custom_providers.save(
            preset_key, preset["label"], preset["base_url"],
            note=preset.get("note", ""), models=models,
            has_models_endpoint=preset.get("has_models") != "no",
            free_tier=preset.get("free_tier") == "yes")
        if result.get("ok"):
            custom_providers.set_api_key(preset_key, api_key)
            saved.append(preset_key)
        else:
            skipped.append(f"{preset['label']}: {result.get('error')}")

    built = await build(AUTO_KEY)
    if built.get("ok") and make_default:
        settings.set("default_route", AUTO_KEY)
    return {"ok": bool(saved) or built.get("ok", False), "saved": saved,
            "skipped": skipped, "route": built}


async def refresh_if_stale() -> dict[str, Any] | None:
    """Called once at start-up, in the background."""
    if not settings.get("auto_route_managed", True):
        return None
    route = router.get_route(AUTO_KEY)
    if route is None:
        return None
    last = float(settings.get("auto_route_built") or 0)
    if route.candidates and time.time() - last < STALE_AFTER:
        return None
    from .providers import custom as custom_providers
    if not custom_providers.stored():
        return None
    try:
        result = await build(AUTO_KEY)
    except Exception as exc:          # never let this break start-up
        return {"ok": False, "error": str(exc)}
    if result.get("ok") and not settings.get("default_route"):
        settings.set("default_route", AUTO_KEY)
    return result
