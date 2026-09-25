"""Tools for models that have no tool calling.

Plenty of free models - and every CLI subscription - answer text only. v2
used to drop the tools for them. v2.1 teaches them a tiny text protocol
instead, the way early agent frameworks did:

    <tool_call>{"name": "web__search", "arguments": {"query": "…"}}</tool_call>

TextToolsProvider wraps any provider. It flattens the tool history into plain
text (every backend accepts that), adds a compact tool list and the protocol to
the system prompt, then watches the stream: prose is passed through as it
arrives, tool-call blocks are held back, parsed, and emitted as a normal
tool_calls event. To the agent loop it is just a provider that supports tools.

Parsing is forgiving because small models are sloppy: fenced JSON inside the
tags, "parameters" instead of "arguments", a trailing comma, a missing closing
tag at the very end of the reply.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator

from .base import Event, Message, Provider, flatten_for_text

OPEN = "<tool_call>"
CLOSE = "</tool_call>"
MAX_TOOLS_LISTED = 60

PROTOCOL = """## Using tools
You can use the tools listed below. To call one, write ONLY this block, with \
valid JSON inside, and nothing after it:
<tool_call>{"name": "TOOL_NAME", "arguments": {"param": "value"}}</tool_call>
You may write several blocks in a row to call several tools. Then STOP - the \
results will come back to you as a message starting with [result of TOOL_NAME]. \
Read them and continue. When you have everything you need, answer normally \
without any tool_call block. Never invent a result."""


def describe(tools: list[Any]) -> str:
    lines = [PROTOCOL, "", "Tools:"]
    for tool in tools[:MAX_TOOLS_LISTED]:
        schema = getattr(tool, "input_schema", {}) or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params = ", ".join(
            f"{name}{'' if name in required else '?'}: {spec.get('type', 'any')}"
            for name, spec in props.items() if isinstance(spec, dict))
        desc = " ".join(str(getattr(tool, "description", "") or "").split())[:180]
        lines.append(f"- {tool.name}({params}) — {desc}")
    if len(tools) > MAX_TOOLS_LISTED:
        lines.append(f"(+{len(tools) - MAX_TOOLS_LISTED} more not listed)")
    return "\n".join(lines)


def _loads(raw: str) -> Any:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)          # trailing commas
    try:
        return json.loads(fixed)
    except ValueError:
        start, end = fixed.find("{"), fixed.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(fixed[start:end + 1])
            except ValueError:
                return None
    return None


def parse_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split a finished reply into (prose, calls)."""
    calls: list[dict[str, Any]] = []
    prose_parts: list[str] = []
    pos = 0
    while True:
        start = text.find(OPEN, pos)
        if start < 0:
            prose_parts.append(text[pos:])
            break
        prose_parts.append(text[pos:start])
        end = text.find(CLOSE, start)
        body = text[start + len(OPEN): end if end >= 0 else len(text)]
        pos = end + len(CLOSE) if end >= 0 else len(text)
        data = _loads(body)
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            args = item.get("arguments", item.get("parameters", {}))
            if isinstance(args, str):
                args = _loads(args) or {"value": args}
            calls.append({"id": f"tc_{uuid.uuid4().hex[:8]}",
                          "name": str(item["name"]),
                          "arguments": args if isinstance(args, dict) else {}})
    return "".join(prose_parts), calls


class TextToolsProvider(Provider):
    """Any provider, plus tools through the text protocol."""

    supports_tools = True

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.key = inner.key
        self.label = inner.label
        self.kind = inner.kind
        self.blurb = inner.blurb
        self.needs = inner.needs
        self.supports_images = inner.supports_images

    async def status(self) -> dict[str, Any]:
        return await self.inner.status()

    async def models(self) -> list[str]:
        return await self.inner.models()

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        tools = opts.pop("tools", None) or []
        if not tools:
            async for event in self.inner.chat(messages, model, **opts):
                yield event
            return
        flat = flatten_for_text(messages)
        block = describe(tools)
        if flat and flat[0].get("role") == "system":
            flat[0] = {"role": "system", "content": f"{flat[0]['content']}\n\n{block}"}
        else:
            flat.insert(0, {"role": "system", "content": block})

        buffer = ""
        emitted = 0              # characters of buffer already passed on
        in_call = False
        async for event in self.inner.chat(flat, model, **opts):
            if event.get("type") != "delta":
                if event.get("type") == "done":
                    continue
                yield event
                continue
            buffer += event.get("text", "")
            if in_call:
                continue
            start = buffer.find(OPEN, emitted)
            if start >= 0:
                if start > emitted:
                    yield {"type": "delta", "text": buffer[emitted:start]}
                emitted = start
                in_call = True
                continue
            # Hold back anything that could be the start of "<tool_call>".
            safe = len(buffer)
            tail = buffer[-(len(OPEN) - 1):]
            for i in range(len(tail)):
                if OPEN.startswith(tail[i:]):
                    safe = len(buffer) - len(tail) + i
                    break
            if safe > emitted:
                yield {"type": "delta", "text": buffer[emitted:safe]}
                emitted = safe

        rest = buffer[emitted:]
        prose, calls = parse_calls(rest)
        if not calls and in_call:
            prose = rest            # nothing parseable: show it rather than lose it
        if prose.strip():
            yield {"type": "delta", "text": prose}
        if calls:
            yield {"type": "tool_calls", "calls": calls}
        yield {"type": "done", "stop_reason": "tool_calls" if calls else "stop"}
