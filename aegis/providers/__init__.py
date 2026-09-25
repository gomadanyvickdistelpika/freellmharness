"""Provider registry.

Rebuilt on demand rather than cached at import, because several providers only
exist once you have configured them - a llama.cpp route appears when a server
is running, Azure appears once you have signed in and named an endpoint.
"""

from __future__ import annotations

from typing import Any

from .. import oauth
from ..config import settings
from . import custom
from .anthropic import AnthropicProvider
from .base import Provider
from .cli import ClaudeCLIProvider, CodexCLIProvider
from .ollama_provider import OllamaProvider
from .openai_compat import (azure_openai_provider, llamacpp_provider,
                            lmstudio_provider, openai_provider)


def registry(include_routes: bool = False) -> dict[str, Provider]:
    """Every backend that can be addressed right now.

    Routes are excluded by default and only added for the chat picker: a route
    is itself made of these providers, so including it here would let a route
    contain itself.
    """
    from .. import runners

    providers: list[Provider] = [
        OllamaProvider(),
        lmstudio_provider(),
        openai_provider(),
        AnthropicProvider(),
        CodexCLIProvider(),
        ClaudeCLIProvider(),
        *custom.build(),
    ]

    if runners.server.running:
        providers.append(llamacpp_provider(runners.server.port))

    azure_cfg = oauth.client_config("microsoft")
    if azure_cfg.get("endpoint"):
        providers.append(azure_openai_provider(
            azure_cfg["endpoint"], lambda: oauth.token_sync("microsoft")))

    result = {p.key: p for p in providers}

    if include_routes:
        from .routed import route_providers
        for routed in route_providers():
            result[routed.key] = routed

    return result


def get(key: str) -> Provider | None:
    return registry(include_routes=str(key).startswith("route:")).get(key)


async def status_all(include_routes: bool = True) -> list[dict[str, Any]]:
    import asyncio

    regs = registry(include_routes=include_routes)
    results = await asyncio.gather(
        *(p.status() for p in regs.values()), return_exceptions=True)
    out = []
    for provider, result in zip(regs.values(), results):
        info = provider.describe()
        if isinstance(result, Exception):
            info |= {"ready": False, "detail": f"probe failed: {result}", "hint": ""}
        else:
            info |= result
        out.append(info)
    return out


__all__ = ["registry", "get", "status_all", "Provider"]
