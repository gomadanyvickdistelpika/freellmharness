"""One implementation for every OpenAI-shaped endpoint.

LM Studio, llama.cpp's llama-server, OpenAI itself, Azure OpenAI, and any
OpenAI-compatible gateway all speak /v1/chat/completions with SSE. They differ
only in base URL and how the Authorization header is produced - so they are
configurations, not separate classes.

Tool calling and image content are both supported; whether a given *model*
honours them is the model's business, and a model that ignores tools simply
never emits a tool_calls event.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

import httpx

from .. import budget, vault
from ..config import LMSTUDIO_HOST
from .base import Event, Message, Provider, not_ok, ok, parse_arguments


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Canonical transcript -> OpenAI wire format.

    Tool results cannot carry images in this API, so any image a tool returned
    is appended as a following user message. That is the standard workaround and
    it keeps the picture in the context where the model can still see it.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")

        if role == "tool":
            out.append({"role": "tool",
                        "tool_call_id": m.get("tool_call_id", ""),
                        "content": str(m.get("content") or "")[:60000]})
            for media_type, data in (m.get("images") or []):
                out.append({"role": "user", "content": [
                    {"type": "text",
                     "text": f"(image returned by {m.get('name', 'the tool')})"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{data}"}},
                ]})
            continue

        if role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [{
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"],
                                 "arguments": json.dumps(c.get("arguments") or {})},
                } for c in m["tool_calls"]],
            })
            continue

        if m.get("images"):
            content: list[dict[str, Any]] = [
                {"type": "text", "text": str(m.get("content") or "")}]
            for media_type, data in m["images"]:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"}})
            out.append({"role": role, "content": content})
            continue

        out.append({"role": role, "content": str(m.get("content") or "")})
    return out


def _retry_after_header(headers: Any) -> float | None:
    """Read Retry-After, which may be seconds or an HTTP date."""
    raw = None
    for name in ("retry-after", "x-ratelimit-reset-after", "ratelimit-reset"):
        if value := headers.get(name):
            raw = value
            break
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        when = parsedate_to_datetime(str(raw))
        delta = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-key cooldowns for providers with several keys (in memory: a restart
# simply tries every key again, which is harmless).
# ---------------------------------------------------------------------------

_KEY_COOL: dict[str, float] = {}


def _kid(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def cool_key(token: str, status: Any, retry_after: Any = None) -> None:
    import time
    seconds = {401: 3600, 403: 3600, 402: 6 * 3600}.get(status, 60)
    if retry_after:
        try:
            seconds = min(float(retry_after), 6 * 3600)
        except (TypeError, ValueError):
            pass
    _KEY_COOL[_kid(token)] = time.time() + seconds


def key_cooling(token: str) -> bool:
    import time
    return _KEY_COOL.get(_kid(token), 0) > time.time()


class OpenAICompatProvider(Provider):
    kind = "api"
    supports_tools = True
    supports_images = True

    def __init__(self, key: str, label: str, base_url: str, *,
                 kind: str = "api", blurb: str = "", needs: str = "api_key",
                 auth: Callable[[], str | None] | None = None,
                 default_models: list[str] | None = None,
                 extra_headers: dict[str, str] | None = None,
                 has_models_endpoint: bool = True) -> None:
        self.key = key
        self.label = label
        self.kind = kind
        self.blurb = blurb
        self.needs = needs
        self.base_url = base_url.rstrip("/")
        self._auth = auth
        self._default_models = default_models or []
        self._extra_headers = extra_headers or {}
        # Not every OpenAI-compatible service implements /models. Atria, for
        # one, documents only /chat/completions - so probing for a model list
        # would report a working provider as broken.
        self.has_models_endpoint = has_models_endpoint

    # -- helpers ----------------------------------------------------------

    def _headers(self, token_override: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        token = token_override or (self._auth() if self._auth else None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # -- contract ---------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        if self.needs == "api_key" and not (self._auth and self._auth()):
            return not_ok("No API key saved",
                          f"Add your {self.label} key on the Providers tab.")
        if self.needs == "oauth" and not (self._auth and self._auth()):
            return not_ok("Not signed in", f"Sign in to {self.label}.")

        if not self.has_models_endpoint:
            # Nothing cheap to call, so do not invent a health check that
            # spends tokens. Say plainly what is and is not known.
            count = len(self._default_models)
            return ok(f"Key saved; {count} model(s) configured by hand "
                      f"(this provider has no /models endpoint to verify against)")

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/models",
                                     headers=self._headers(), timeout=5.0)
            if r.status_code == 401:
                return not_ok("Credentials rejected (401)",
                              "The saved key or token is wrong or expired.")
            if r.status_code >= 400:
                return not_ok(f"HTTP {r.status_code}", "")
            n = len(r.json().get("data", []))
            return ok(f"{n} model{'s' if n != 1 else ''} available")
        except Exception as exc:
            hint = ("Start LM Studio and turn on its local server."
                    if self.key == "lmstudio" else "")
            return not_ok(f"Not reachable: {type(exc).__name__}", hint)

    async def models(self) -> list[str]:
        if not self.has_models_endpoint:
            return self._default_models
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/models",
                                     headers=self._headers(), timeout=8.0)
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
                return sorted(ids) or self._default_models
        except Exception:
            return self._default_models

    async def chat(self, messages: list[Message], model: str,
                   **opts: Any) -> AsyncIterator[Event]:
        """v2.1: with several keys saved for one provider (FreeLLMAPI-style),
        a key that is rate-limited, out of allowance or rejected is cooled
        down and the next key is tried *before* the router moves to another
        model. One key behaves exactly as before."""
        keys = self._rotation()
        if len(keys) <= 1:
            token = keys[0] if keys else None
            async for event in self._chat_once(token, messages, model, **opts):
                yield event
            return
        last_error: Event | None = None
        for index, token in enumerate(keys):
            stream = self._chat_once(token, messages, model, **opts)
            first = await stream.__anext__()
            if first.get("type") == "error" and first.get("status") in (401, 402, 403, 429) \
                    and index < len(keys) - 1:
                cool_key(token, first.get("status"), first.get("retry_after"))
                last_error = first
                await stream.aclose()
                continue
            if first.get("type") == "error" and first.get("status") in (401, 402, 403, 429):
                cool_key(token, first.get("status"), first.get("retry_after"))
            yield first
            async for event in stream:
                yield event
            return
        if last_error:
            yield last_error

    def _rotation(self) -> list[str]:
        getter = getattr(self, "_keys", None)
        keys = list(getter() if getter else [])
        if not keys:
            return []
        ready = [k for k in keys if not key_cooling(k)]
        return ready or keys[:1]

    async def _chat_once(self, token: str | None, messages: list[Message],
                         model: str, **opts: Any) -> AsyncIterator[Event]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "temperature": opts.get("temperature", 0.7),
        }
        if opts.get("max_tokens"):
            payload["max_tokens"] = int(opts["max_tokens"])
        if tools := opts.get("tools"):
            payload["tools"] = [t.openai_schema() for t in tools]
            payload["tool_choice"] = "auto"

        pending: dict[int, dict[str, Any]] = {}
        stop_reason = "stop"

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions",
                    headers=self._headers(token), json=payload,
                    timeout=httpx.Timeout(30.0, read=None),
                ) as resp:
                    # Every response, success or failure, may carry what is
                    # left on this key. Reading it is how AEGIS steps off a
                    # model before the 429 rather than after it.
                    try:
                        budget.note_headers(self.key, model, resp.headers)
                    except Exception:
                        pass    # never let bookkeeping break a reply
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:600]
                        # The status travels with the event so the router can
                        # classify the failure properly instead of regexing text.
                        event: dict[str, Any] = {
                            "type": "error", "status": resp.status_code,
                            "text": f"HTTP {resp.status_code}: {body}"}
                        # Providers that tell you when to come back are worth
                        # listening to - it beats any cooldown we would invent.
                        if hint := _retry_after_header(resp.headers):
                            event["retry_after"] = hint
                        if remaining := resp.headers.get("x-rpm-remaining"):
                            event["text"] += f" [rpm remaining: {remaining}]"
                        yield event
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue

                        # Some gateways (OpenRouter among them) report a
                        # failure *inside* a 200 stream. Surface it with its
                        # code so the router can fail over instead of the
                        # reply silently ending empty.
                        if isinstance(chunk, dict) and chunk.get("error"):
                            err = chunk["error"]
                            msg = err.get("message") if isinstance(err, dict) else str(err)
                            code = err.get("code") if isinstance(err, dict) else None
                            try:
                                code = int(code) if code is not None else None
                            except (TypeError, ValueError):
                                code = None
                            yield {"type": "error", "status": code,
                                   "text": f"stream error: {msg}"[:600]}
                            return

                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            if piece := delta.get("content"):
                                yield {"type": "delta", "text": piece}
                            for call in delta.get("tool_calls") or []:
                                idx = int(call.get("index", 0))
                                slot = pending.setdefault(
                                    idx, {"id": "", "name": "", "arguments": ""})
                                if cid := call.get("id"):
                                    slot["id"] = cid
                                fn = call.get("function") or {}
                                if name := fn.get("name"):
                                    slot["name"] = name
                                if args := fn.get("arguments"):
                                    slot["arguments"] += args
                            if reason := choice.get("finish_reason"):
                                stop_reason = reason

                        if usage := chunk.get("usage"):
                            yield {"type": "usage",
                                   "input": usage.get("prompt_tokens", 0),
                                   "output": usage.get("completion_tokens", 0)}

            if pending:
                yield {"type": "tool_calls", "calls": [
                    {"id": slot["id"] or f"call_{idx}",
                     "name": slot["name"],
                     "arguments": parse_arguments(slot["arguments"])}
                    for idx, slot in sorted(pending.items()) if slot["name"]
                ]}
            yield {"type": "done", "stop_reason": stop_reason}
        except Exception as exc:
            yield {"type": "error", "text": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Concrete configurations
# ---------------------------------------------------------------------------

def openai_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        key="openai",
        label="OpenAI API",
        base_url="https://api.openai.com/v1",
        blurb="Pay-per-token API key. Separate from a ChatGPT subscription.",
        needs="api_key",
        auth=lambda: vault.resolve_api_key("openai_api_key", "OPENAI_API_KEY"),
        default_models=["gpt-4o", "gpt-4o-mini"],
    )


def lmstudio_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        key="lmstudio",
        label="LM Studio",
        base_url=f"{LMSTUDIO_HOST}/v1",
        kind="local",
        blurb="Whatever LM Studio is serving on this machine. No key needed.",
        needs="",
        auth=None,
    )


def llamacpp_provider(port: int) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        key="llamacpp",
        label="llama.cpp (AEGIS)",
        base_url=f"http://127.0.0.1:{port}/v1",
        kind="local",
        blurb="A llama-server AEGIS started for a GGUF it downloaded.",
        needs="",
        auth=None,
    )


def azure_openai_provider(endpoint: str, token_getter) -> OpenAICompatProvider:
    """Azure OpenAI with an Entra ID bearer token - real third-party OAuth."""
    return OpenAICompatProvider(
        key="azure_openai",
        label="Azure OpenAI (Entra sign-in)",
        base_url=f"{endpoint.rstrip('/')}/openai/v1",
        blurb="OpenAI models via your Microsoft account. Browser sign-in, no key.",
        needs="oauth",
        auth=token_getter,
    )
