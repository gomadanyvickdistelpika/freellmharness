"""Paths, constants and persisted settings for AEGIS.

Everything AEGIS writes lives under one directory so it is trivial to back up,
move to another machine, or delete entirely:

    Windows : %LOCALAPPDATA%\\Aegis
    Linux   : ~/.local/share/aegis
    macOS   : ~/Library/Application Support/Aegis
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

APP_NAME = "AEGIS"
APP_SLUG = "aegis"
VERSION = "2.2.0"

# Where the local API server binds. Loopback only - never 0.0.0.0.
HOST = "127.0.0.1"
DEFAULT_PORT = 8817

# Local engines AEGIS knows how to talk to out of the box.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
LMSTUDIO_HOST = os.environ.get("LMSTUDIO_HOST", "http://127.0.0.1:1234")

HF_API = "https://huggingface.co/api"
HF_HOST = "https://huggingface.co"


def _data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Aegis"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aegis"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_SLUG


DATA_DIR = Path(os.environ.get("AEGIS_DATA_DIR") or _data_root())
MODELS_DIR = DATA_DIR / "models"          # GGUF files AEGIS downloads itself
BIN_DIR = DATA_DIR / "bin"                # llama-server and friends
LOG_DIR = DATA_DIR / "logs"
VAULT_PATH = DATA_DIR / "vault.bin"       # encrypted secrets
SETTINGS_PATH = DATA_DIR / "settings.json"
CHATS_PATH = DATA_DIR / "chats.json"

WEB_DIR = Path(__file__).parent / "web"


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODELS_DIR, BIN_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


DEFAULTS: dict[str, Any] = {
    # Context length used for the memory-fit estimate. LM Studio does the same:
    # the verdict changes as you drag this up.
    "fit_context": 4096,
    # Leave this much system RAM for Windows and everything else, in MB.
    "ram_headroom_mb": 2048,
    # Treat integrated GPUs (Iris Xe, Vega, UHD) as system RAM rather than VRAM,
    # because that is what they actually are.
    "igpu_counts_as_ram": True,
    "default_provider": "ollama",
    "default_model": "",
    "theme": "dark",
    # --- tools, MCP and computer use ---
    # How much the agent may do on its own before asking:
    #   auto_read_ask_write | ask_always | auto_all | per_server
    "tool_policy": "auto_read_ask_write",
    "tool_decisions": {},      # tool name -> "allow" | "deny", remembered
    "server_policies": {},     # server name -> policy, when tool_policy is per_server
    "mcp_servers": {},         # name -> {spec, origin, enabled}
    # Computer and browser use ship switched on - every action they take is
    # still classified as a write, so the approval gate is what protects you,
    # not an off switch you have to remember to flip.
    "computer_use_enabled": True,
    "browser_use_enabled": True,
    # Which browser an agent gets. "aegis" is its own Chromium with its own
    # profile; "chrome" attaches to yours over CDP and is only ever chosen
    # deliberately, because that browser holds your live sessions.
    "browser_target": "aegis",
    "browser_prefer_chrome": False,    # legacy; migrated into browser_target
    "browser_cdp_url": "",             # blank means http://127.0.0.1:9222
    "browser_headless": False,         # False so you can see and grab the window
    "subagents_enabled": True,
    "vault_path": "",                  # your Obsidian folder; indexed read-only
    "max_steps": 15,           # hard stop for the agent loop
    # --- files and scheduling ---
    "folders": [],             # absolute paths the file tools may touch
    "tasks": {},               # key -> scheduled task definition
    # --- model routing ---
    "routes": {},              # key -> {label, candidates, allow_paid}
    "custom_providers": {},    # key -> {label, base_url, note, headers}
    "free_models": [],         # "provider::model" entries known to cost nothing
    "model_health": {},        # "provider::model" -> cooldowns and speed
    # Budgets: step off a model before it 429s rather than after.
    "model_budgets": {},       # "provider::model" or "provider::*" -> rpm/rpd/tpd
    "learned_budgets": {},     # same keys -> limits implied by response headers
    "budget_usage": {},        # "provider::model" -> what today has cost
    "default_route": "",       # a route key, or "" to use provider+model directly
    # --- chat persistence ---
    # Per-chat image allowance. One computer-use session can produce fifty
    # screenshots; past this the oldest are evicted and the trace says so.
    "chat_image_budget_mb": 48,
    # Filled in by the OAuth tab. Client IDs are not secrets in a PKCE flow,
    # but they are per-user, so they live in settings rather than the code.
    "oauth_clients": {},
    # --- v2.2: Needle, the offline fast-intent step (needle_intent.py) ---
    # Short commands ("open youtube", "boost off") become slash commands on
    # the CPU in milliseconds, before any model is asked. No-op until
    # `pip install cactus-needle`; acts only above this confidence.
    "needle_enabled": True,
    "needle_min_confidence": 0.6,
    "needle_max_chars": 160,
    "needle_weights": "",      # optional path to your own fine-tuned .cact
}


class _Settings:
    """Tiny JSON-backed settings store. Thread-safe, writes atomically."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            raw = json.loads(SETTINGS_PATH.read_text("utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
        except (OSError, ValueError):
            pass
        self._loaded = True

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._load()
            return self._data.get(key, DEFAULTS.get(key, default))

    def all(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return dict(self._data)

    def set(self, key: str, value: Any) -> None:
        self.update({key: value})

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._load()
            self._data.update(values)
            ensure_dirs()
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            tmp.replace(SETTINGS_PATH)


settings = _Settings()
