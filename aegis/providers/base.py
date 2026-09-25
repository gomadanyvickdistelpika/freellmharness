"""Provider contract.

Every backend - a local llama.cpp server, the Anthropic API, a CLI subprocess -
looks the same to the rest of AEGIS:

    status()   is this usable right now, and if not, why not
    models()   what can I ask for
    chat()     an async stream of events

Events are dicts so new kinds can be added without breaking consumers:

    {"type": "delta",      "text": "..."}          incremental output
    {"type": "notice",     "text": "..."}          progress from a CLI
    {"type": "tool_calls", "calls": [...]}         the model wants tools run
    {"type": "usage",      "input": n, "output": n}
    {"type": "error",      "text": "..."}
    {"type": "done",       "stop_reason": "..."}

Canonical messages
------------------
The agent loop keeps one provider-neutral transcript; each provider serialises
it into its own wire format. The shapes are:

    {"role": "system"|"user", "content": str}
    {"role": "user", "content": str, "images": [(media_type, b64), ...]}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool", "tool_call_id": str, "name": str,
     "content": str, "images": [...], "is_error": bool}

where ToolCall is {"id": str, "name": str, "arguments": dict}.
"""

from __future__ import annotations

import abc
import json
from typing import Any, AsyncIterator

Message = dict[str, Any]
Event = dict[str, Any]


class ProviderError(Exception):
    pass


class Provider(abc.ABC):
    key: str = ""
    label: str = ""
    kind: str = "api"             # local | api | cli
    blurb: str = ""
    needs: str = ""               # "" | "api_key" | "oauth" | "cli"
    supports_tools: bool = False
    supports_images: bool = False

    @abc.abstractmethod
    async def status(self) -> dict[str, Any]:
        """{'ready': bool, 'detail': str, 'hint': str}"""

    @abc.abstractmethod
    async def models(self) -> list[str]:
        ...

    @abc.abstractmethod
    def chat(self, messages: list[Message], model: str,
             **opts: Any) -> AsyncIterator[Event]:
        ...

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "blurb": self.blurb, "needs": self.needs,
                "supports_tools": self.supports_tools,
                "supports_images": self.supports_images}


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Anthropic wants the system prompt out of band. Pull it out."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages
                         if m.get("role") == "system" and m.get("content"))
    rest = [m for m in messages if m.get("role") != "system"]
    return system, rest


def flatten_for_text(messages: list[Message]) -> list[Message]:
    """Drop tool machinery for backends that cannot take it.

    Tool calls and results become plain text so the conversation still reads
    correctly rather than losing steps silently.
    """
    out: list[Message] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "user",
                        "content": f"[result of {m.get('name', 'tool')}]\n"
                                   f"{m.get('content', '')}"})
        elif role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(c["name"] for c in m["tool_calls"])
            text = (m.get("content") or "").strip()
            out.append({"role": "assistant",
                        "content": (text + f"\n[called: {calls}]").strip()})
        else:
            out.append({"role": role, "content": str(m.get("content") or "")})
    return out


def ok(detail: str = "") -> dict[str, Any]:
    return {"ready": True, "detail": detail, "hint": ""}


def not_ok(detail: str, hint: str = "") -> dict[str, Any]:
    return {"ready": False, "detail": detail, "hint": hint}


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Models emit arguments as a JSON string, sometimes malformed."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (ValueError, TypeError):
        return {"__unparsed__": str(raw)[:2000]}
