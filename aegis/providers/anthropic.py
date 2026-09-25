"""Anthropic Messages API, with tool use and image content."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .. import vault
from .base import Event, Message, Provider, not_ok, ok, split_system

API_VERSION = "2023-06-01"

FALLBACK_MODELS = [
    "claude-sonnet-4-5",
    "claude-opus-4-1",
    "claude-haiku-4-5",
]


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Canonical transcript -> Anthropic wire format.

    Anthropic takes tool results as content blocks on a *user* message, and
    unlike OpenAI it accepts images inside a tool_result, so a screenshot stays
    attached to the call that produced it. Consecutive tool results are merged
    into one user message, which the API requires.
    """
    out: list[dict[str, Any]] = []

    for m in messages:
        role = m.get("role")

        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": [{"type": "text",
                             "text": str(m.get("content") or "")[:60000]}],
            }
            for media_type, data in (m.get("images") or []):
                block["content"].append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type,
                               "data": data},
                })
            if m.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and m.get("tool_calls"):
            content: list[dict[str, Any]] = []
            if text := (m.get("content") or "").strip():
                content.append({"type": "text", "text": text})
            for call in m["tool_calls"]:
                content.append({"type": "tool_use", "id": call["id"],
                                "name": call["name"],
                                "input": call.get("arguments") or {}})
            out.append({"role": "assistant", "content": content})
            continue

        if m.get("images"):
            content = [{"type": "text", "text": str(m.get("content") or "")}]
            for media_type, data in m["images"]:
                content.append({"type": "image",
                                "source": {"type": "base64",
                                           "media_type": media_type, "data": data}})
            out.append({"role": role, "content": content})
            continue

        out.append({"role": role, "content": str(m.get("content") or "")})
    return out


class AnthropicProvider(Provider):
    key = "anthropic"
    label = "Anthropic API"
    kind = "api"
    needs = "api_key"
    blurb = "Pay-per-token API key. Separate from a Claude subscription."
    supports_tools = True
    supports_images = True

    base_url = "https://api.anthropic.com/v1"

    def _key(self) -> str | None:
        return vault.resolve_api_key("anthropic_api_key", "ANTHROPIC_API_KEY")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key() or "",
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }

    async def status(self) -> dict[str, Any]:
        if not self._key():
            return not_ok("No API key saved",
                          "Add your Anthropic key on the Providers tab.")
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/models",
                                     headers=self._headers(), timeout=6.0)
            if r.status_code == 401:
                return not_ok("Key rejected (401)", "The saved key is wrong or revoked.")
            if r.status_code >= 400:
                return not_ok(f"HTTP {r.status_code}", "")
            return ok(f"{len(r.json().get('data', []))} models available")
        except Exception as exc:
            return not_ok(f"Not reachable: {type(exc).__name__}", "")

    async def models(self) -> list[str]:
        if not self._key():
            return FALLBACK_MODELS
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/models",
                                     headers=self._headers(),
                                     params={"limit": "100"}, timeout=8.0)
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
                return ids or FALLBACK_MODELS
        except Exception:
            return FALLBACK_MODELS

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        system, convo = split_system(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": to_anthropic_messages(convo),
            "max_tokens": int(opts.get("max_tokens") or 4096),
            "stream": True,
        }
        if system:
            payload["system"] = system
        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        if tools := opts.get("tools"):
            payload["tools"] = [t.anthropic_schema() for t in tools]

        blocks: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", f"{self.base_url}/messages",
                    headers=self._headers(), json=payload,
                    timeout=httpx.Timeout(30.0, read=None),
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:600]
                        yield {"type": "error", "status": resp.status_code,
                               "text": f"HTTP {resp.status_code}: {body}"}
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        etype = evt.get("type")

                        if etype == "content_block_start":
                            block = evt.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                blocks[int(evt.get("index", 0))] = {
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "json": "",
                                }
                        elif etype == "content_block_delta":
                            delta = evt.get("delta") or {}
                            if text := delta.get("text"):
                                yield {"type": "delta", "text": text}
                            if partial := delta.get("partial_json"):
                                slot = blocks.get(int(evt.get("index", 0)))
                                if slot is not None:
                                    slot["json"] += partial
                        elif etype == "message_delta":
                            if reason := (evt.get("delta") or {}).get("stop_reason"):
                                stop_reason = reason
                            if usage := evt.get("usage"):
                                yield {"type": "usage", "input": 0,
                                       "output": usage.get("output_tokens", 0)}
                        elif etype == "error":
                            err = evt.get("error") or {}
                            yield {"type": "error",
                                   "text": err.get("message", "unknown error")}
                            return

            if blocks:
                calls = []
                for idx, slot in sorted(blocks.items()):
                    try:
                        args = json.loads(slot["json"]) if slot["json"].strip() else {}
                    except ValueError:
                        args = {"__unparsed__": slot["json"][:2000]}
                    calls.append({"id": slot["id"] or f"toolu_{idx}",
                                  "name": slot["name"],
                                  "arguments": args if isinstance(args, dict) else {}})
                yield {"type": "tool_calls", "calls": calls}

            yield {"type": "done", "stop_reason": stop_reason}
        except Exception as exc:
            yield {"type": "error", "text": f"{type(exc).__name__}: {exc}"}
