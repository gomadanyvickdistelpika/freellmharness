"""The tool surface the model sees.

Five sources, merged into one namespaced catalogue:

    <server>__<tool>   MCP servers you enabled
    computer__*        screenshot, mouse, keyboard
    browser__*         page-level browser control
    memory__*          agent memory and your vault index
    skill__*           load a skill, or draft a new one
    code__*            run commands and Python in the workspace      (v2)
    web__*             search the web and read pages                 (v2)
    media__*           images, speech, music, video                  (v2)
    project__*         projects and scheduled tasks                  (v2)

agent__spawn is not here: it needs the provider the current run is using, so it
is added per run in runs.py. That is also why a subagent cannot spawn another -
this catalogue, which is what a subagent gets, has no spawn tool in it.
"""

from __future__ import annotations

from typing import Any

from . import browser, computer
from . import files as files_tools
from .approval import gate
from .base import READ, WRITE, Tool, classify, namespace


def catalogue(extra: list[Tool] | None = None) -> list[Tool]:
    """Everything callable right now, in a stable order."""
    from .. import memory, skills
    from ..mcp.registry import registry

    from .. import boost, custom_agents, media, personal, projects, voice
    from ..mcp import catalog as mcp_catalog
    from . import code, web

    tools: list[Tool] = [
        *registry.catalogue(),
        *computer.tools(),
        *browser.tools(),
        *files_tools.tools(),
        *code.tools(),
        *web.tools(),
        *media.tools(),
        *projects.tools(),
        *personal.tools(),
        *boost.plan_tools(),
        *custom_agents.tools(),
        *mcp_catalog.tools(),
        *voice.tools(),
        *memory.tools(),
        *skills.tools(),
        *(extra or []),
    ]
    tools.sort(key=lambda t: (t.server, t.name))
    return tools


def by_name(extra: list[Tool] | None = None) -> dict[str, Tool]:
    return {t.name: t for t in catalogue(extra)}


def summary() -> dict[str, Any]:
    tools = catalogue()
    return {
        "total": len(tools),
        "read": sum(1 for t in tools if t.risk == READ),
        "write": sum(1 for t in tools if t.risk == WRITE),
        "servers": sorted({t.server for t in tools}),
        "tools": [t.to_dict() for t in tools],
    }


__all__ = ["catalogue", "by_name", "summary", "gate", "Tool", "classify",
           "namespace", "READ", "WRITE", "computer", "browser", "files_tools"]
