"""Ollama's native /api/chat endpoint, with tool calling.

Ollama also exposes an OpenAI-compatible surface, but the native one reports
load progress and eval counts, which are worth having on a machine where a
model load takes real time.

Tool calling depends on the *model*, not on Ollama - qwen2.5, llama3.1 and
mistral handle it; most sub-3B models will ignore the tools and just talk. That
is a model limitation, and the agent loop reports it rather than looping.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import OLLAMA_HOST
from .base import Event, Message, Provider, not_ok, ok, parse_arguments


def to_ollama_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Ollama takes OpenAI-ish messages, with images as a sibling b64 list."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")

        if role == "tool":
            entry: dict[str, Any] = {"role": "tool",
                                     "content": str(m.get("content") or "")[:60000]}
            if name := m.get("name"):
                entry["tool_name"] = name
            out.append(entry)
            for _media_type, data in (m.get("images") or []):
                out.append({"role": "user",
                            "content": "(image returned by the tool)",
                            "images": [data]})
            continue

        if role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": [{"function": {"name": c["name"],
                                             "arguments": c.get("arguments") or {}}}
                               for c in m["tool_calls"]],
            })
            continue

        entry = {"role": role, "content": str(m.get("content") or "")}
        if images := m.get("images"):
            entry["images"] = [data for _mt, data in images]
        out.append(entry)
    return out


class OllamaProvider(Provider):
    key = "ollama"
    label = "Ollama"
    kind = "local"
    needs = ""
    blurb = "Local models via Ollama. Nothing leaves this machine."
    supports_tools = True
    supports_images = True

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=4.0)
                r.raise_for_status()
                models = r.json().get("models", [])
            if not models:
                return ok("Running, but no models pulled yet")
            return ok(f"{len(models)} model{'s' if len(models) != 1 else ''} installed")
        except Exception:
            return not_ok("Ollama is not responding",
                          "Start Ollama, or install it from ollama.com.")

    async def models(self) -> list[str]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=6.0)
                r.raise_for_status()
                return sorted(m["name"] for m in r.json().get("models", [])
                              if m.get("name"))
        except Exception:
            return []

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        options: dict[str, Any] = {"temperature": opts.get("temperature", 0.7)}
        if opts.get("context"):
            options["num_ctx"] = int(opts["context"])
        if opts.get("max_tokens"):
            options["num_predict"] = int(opts["max_tokens"])

        payload: dict[str, Any] = {
            "model": model,
            "messages": to_ollama_messages(messages),
            "stream": True,
            "options": options,
        }
        if tools := opts.get("tools"):
            payload["tools"] = [t.openai_schema() for t in tools]

        calls: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", f"{OLLAMA_HOST}/api/chat",
                                         json=payload,
                                         timeout=httpx.Timeout(30.0, read=None)) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:400]
                        yield {"type": "error", "status": resp.status_code,
                               "text": f"Ollama HTTP {resp.status_code}: {body}"}
                        return
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except ValueError:
                            continue
                        if err := evt.get("error"):
                            yield {"type": "error", "text": err}
                            return

                        message = evt.get("message") or {}
                        if piece := message.get("content"):
                            yield {"type": "delta", "text": piece}
                        for call in message.get("tool_calls") or []:
                            fn = call.get("function") or {}
                            if name := fn.get("name"):
                                calls.append({
                                    "id": call.get("id") or f"call_{len(calls)}",
                                    "name": name,
                                    "arguments": parse_arguments(fn.get("arguments")),
                                })
                        if evt.get("done"):
                            yield {"type": "usage",
                                   "input": evt.get("prompt_eval_count", 0),
                                   "output": evt.get("eval_count", 0)}

            if calls:
                yield {"type": "tool_calls", "calls": calls}
            yield {"type": "done", "stop_reason": "tool_use" if calls else "stop"}
        except Exception as exc:
            yield {"type": "error", "text": f"{type(exc).__name__}: {exc}"}
