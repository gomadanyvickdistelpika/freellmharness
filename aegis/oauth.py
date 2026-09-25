"""OAuth 2.0 Authorization Code flow with PKCE.

Real browser sign-in, no API keys, tokens encrypted at rest and refreshed
automatically. AEGIS is a public client, so PKCE (RFC 7636) is mandatory and
there is no client secret anywhere.

The redirect lands back on AEGIS's own server:

    http://127.0.0.1:8817/oauth/callback

Register exactly that URI when you create the OAuth app, and paste the client
id into the Providers tab. A client id is not a secret in a PKCE flow, but it
is yours, so it lives in settings rather than baked into this file.

--------------------------------------------------------------------------
A straight word about ChatGPT and Claude subscriptions
--------------------------------------------------------------------------
Neither OpenAI nor Anthropic issues third-party OAuth clients for consumer
ChatGPT or Claude subscriptions. The "Sign in with ChatGPT" in Codex CLI and
the equivalent in Claude Code use client ids registered to those apps. Copying
them into your own harness impersonates a first-party client, breaches both
vendors' terms, and breaks whenever they rotate the id.

So AEGIS does not do that. For those two subscriptions it drives the CLIs you
already have signed in (see providers/cli.py). The OAuth flow here is pointed
at providers that genuinely support third-party clients - and that includes
first-party OpenAI and Anthropic *models* via Azure and Vertex.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import vault
from .config import DEFAULT_PORT, HOST, settings

REDIRECT_PATH = "/oauth/callback"
STATE_TTL = 600  # seconds


def redirect_uri(port: int = DEFAULT_PORT) -> str:
    return f"http://{HOST}:{port}{REDIRECT_PATH}"


@dataclass
class OAuthSpec:
    key: str
    label: str
    auth_url: str
    token_url: str
    scopes: list[str]
    blurb: str = ""
    # Fields the user must supply before this provider can be used.
    config_fields: list[dict[str, str]] = field(default_factory=list)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    def urls(self, cfg: dict[str, str]) -> tuple[str, str]:
        """Auth and token endpoints, with any {tenant}-style holes filled in."""
        auth = self.auth_url.format(**{k: cfg.get(k, "") for k in _keys(self.auth_url)})
        token = self.token_url.format(**{k: cfg.get(k, "") for k in _keys(self.token_url)})
        return auth, token


def _keys(template: str) -> list[str]:
    import string
    return [f for _, f, _, _ in string.Formatter().parse(template) if f]


# ---------------------------------------------------------------------------
# Built-in providers that really do issue third-party OAuth clients
# ---------------------------------------------------------------------------

SPECS: dict[str, OAuthSpec] = {
    "huggingface": OAuthSpec(
        key="huggingface",
        label="Hugging Face",
        auth_url="https://huggingface.co/oauth/authorize",
        token_url="https://huggingface.co/oauth/token",
        scopes=["openid", "profile", "read-repos"],
        blurb=("Signs model downloads. Needed for gated repos such as Llama and "
               "Gemma, and lifts anonymous rate limits."),
        config_fields=[
            {"name": "client_id", "label": "Client ID",
             "hint": "huggingface.co → Settings → Connected Applications → Create"},
        ],
    ),
    "microsoft": OAuthSpec(
        key="microsoft",
        label="Microsoft Entra → Azure OpenAI",
        auth_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scopes=["https://cognitiveservices.azure.com/.default", "offline_access"],
        blurb=("GPT models through an Azure OpenAI resource, authorised by your "
               "Microsoft account instead of an API key."),
        config_fields=[
            {"name": "client_id", "label": "Application (client) ID",
             "hint": "Entra admin centre → App registrations → New registration"},
            {"name": "tenant", "label": "Directory (tenant) ID",
             "hint": "Use 'common' for a personal or multi-tenant account"},
            {"name": "endpoint", "label": "Azure OpenAI endpoint",
             "hint": "https://YOUR-RESOURCE.openai.azure.com"},
        ],
    ),
    "google": OAuthSpec(
        key="google",
        label="Google → Vertex AI",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        blurb=("Claude and Gemini models through Vertex AI on your own Google "
               "Cloud project."),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        config_fields=[
            {"name": "client_id", "label": "OAuth client ID",
             "hint": "Google Cloud console → Credentials → OAuth client → Desktop app"},
            {"name": "project", "label": "GCP project ID", "hint": ""},
            {"name": "location", "label": "Region", "hint": "e.g. europe-west1"},
        ],
    ),
}


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

_pending: dict[str, dict[str, Any]] = {}


def _prune() -> None:
    now = time.time()
    for state in [s for s, v in _pending.items() if now - v["ts"] > STATE_TTL]:
        _pending.pop(state, None)


def client_config(key: str) -> dict[str, str]:
    return dict((settings.get("oauth_clients") or {}).get(key) or {})


def save_client_config(key: str, cfg: dict[str, str]) -> None:
    clients = dict(settings.get("oauth_clients") or {})
    clients[key] = {k: str(v).strip() for k, v in cfg.items() if str(v).strip()}
    settings.set("oauth_clients", clients)


def missing_config(key: str) -> list[str]:
    spec = SPECS.get(key)
    if not spec:
        return ["unknown provider"]
    cfg = client_config(key)
    return [f["label"] for f in spec.config_fields if not cfg.get(f["name"])]


def begin(key: str, port: int = DEFAULT_PORT) -> str:
    """Returns the URL to open in the browser."""
    spec = SPECS.get(key)
    if not spec:
        raise ValueError(f"Unknown OAuth provider: {key}")
    cfg = client_config(key)
    if missing := missing_config(key):
        raise ValueError(f"Fill in first: {', '.join(missing)}")

    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    _prune()
    _pending[state] = {"key": key, "verifier": verifier,
                       "ts": time.time(), "port": port}

    auth_url, _ = spec.urls(cfg)
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri(port),
        "scope": " ".join(spec.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **spec.extra_auth_params,
    }
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


async def complete(code: str, state: str) -> tuple[bool, str]:
    _prune()
    pending = _pending.pop(state, None)
    if not pending:
        return False, "This sign-in link expired or was already used. Try again."

    key = pending["key"]
    spec = SPECS[key]
    cfg = client_config(key)
    _, token_url = spec.urls(cfg)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(pending["port"]),
        "client_id": cfg["client_id"],
        "code_verifier": pending["verifier"],
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(token_url, data=data, timeout=20.0,
                                  headers={"Accept": "application/json"})
        if r.status_code >= 400:
            return False, f"Token exchange failed (HTTP {r.status_code}): {r.text[:300]}"
        tok = r.json()
    except Exception as exc:
        return False, f"Token exchange failed: {exc}"

    _store(key, tok)
    return True, f"Signed in to {spec.label}."


def _store(key: str, tok: dict[str, Any]) -> None:
    expires_in = int(tok.get("expires_in") or 3600)
    record = {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + expires_in - 60,   # refresh a minute early
        "scope": tok.get("scope", ""),
        "token_type": tok.get("token_type", "Bearer"),
    }
    existing = vault.get(f"oauth:{key}") or {}
    if not record["refresh_token"] and existing.get("refresh_token"):
        record["refresh_token"] = existing["refresh_token"]   # some issuers omit it
    vault.put(f"oauth:{key}", record)


async def _refresh(key: str) -> bool:
    record = vault.get(f"oauth:{key}") or {}
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        return False
    spec = SPECS[key]
    cfg = client_config(key)
    _, token_url = spec.urls(cfg)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(token_url, timeout=20.0,
                                  headers={"Accept": "application/json"},
                                  data={"grant_type": "refresh_token",
                                        "refresh_token": refresh_token,
                                        "client_id": cfg.get("client_id", "")})
        if r.status_code >= 400:
            return False
        _store(key, r.json())
        return True
    except Exception:
        return False


async def access_token(key: str) -> str | None:
    """Valid access token, refreshed if needed. None if not signed in."""
    record = vault.get(f"oauth:{key}") or {}
    if not record.get("access_token"):
        return None
    if time.time() < float(record.get("expires_at") or 0):
        return record["access_token"]
    if await _refresh(key):
        return (vault.get(f"oauth:{key}") or {}).get("access_token")
    return None


def token_sync(key: str) -> str | None:
    """Non-refreshing read, for sync call sites like header builders."""
    record = vault.get(f"oauth:{key}") or {}
    token = record.get("access_token")
    if token and time.time() < float(record.get("expires_at") or 0):
        return token
    return token or None


def sign_out(key: str) -> None:
    vault.delete(f"oauth:{key}")


def status_all() -> list[dict[str, Any]]:
    out = []
    for key, spec in SPECS.items():
        record = vault.get(f"oauth:{key}") or {}
        signed_in = bool(record.get("access_token"))
        expires_at = float(record.get("expires_at") or 0)
        out.append({
            "key": key,
            "label": spec.label,
            "blurb": spec.blurb,
            "signed_in": signed_in,
            "expired": signed_in and time.time() >= expires_at,
            "can_refresh": bool(record.get("refresh_token")),
            "scopes": spec.scopes,
            "config_fields": spec.config_fields,
            "config": {k: v for k, v in client_config(key).items()},
            "missing": missing_config(key),
            "redirect_uri": redirect_uri(),
        })
    return out
