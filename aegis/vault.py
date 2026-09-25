"""Encrypted local store for API keys and OAuth tokens.

Two layers, picked automatically:

  Windows  DPAPI (CryptProtectData). The ciphertext is bound to your Windows
           user account - copying vault.bin to another machine or another user
           profile gives them nothing. No passphrase to type. Called through
           ctypes so there is no pywin32 dependency.

  Other    Fernet (AES-128-CBC + HMAC) with a key file at 0600. Weaker, because
           anything running as you can read the key file - stated plainly rather
           than dressed up.

Nothing here is a substitute for Bitwarden for your *important* credentials.
This is a convenience store for machine-local API tokens.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from typing import Any

from .config import DATA_DIR, VAULT_PATH, ensure_dirs

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Windows DPAPI via ctypes
# --------------------------------------------------------------------------

def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(encrypt: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def from_blob(blob: DATA_BLOB) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    src = to_blob(data)
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    # entropy=None, reserved=None, prompt=None, flags=CRYPTPROTECT_UI_FORBIDDEN
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI {'encrypt' if encrypt else 'decrypt'} failed "
                      f"(error {ctypes.GetLastError()})")
    try:
        return from_blob(out)
    finally:
        kernel32.LocalFree(out.pbData)


# --------------------------------------------------------------------------
# Fernet fallback
# --------------------------------------------------------------------------

_KEY_PATH = DATA_DIR / "vault.key"


def _fernet():
    from cryptography.fernet import Fernet

    ensure_dirs()
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _KEY_PATH.write_bytes(key)
        try:
            os.chmod(_KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def backend_name() -> str:
    return "Windows DPAPI" if _dpapi_available() else "Fernet key file"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _encrypt(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload).encode("utf-8")
    if _dpapi_available():
        return b"DPAPI1" + _dpapi(True, raw)
    return b"FERNT1" + _fernet().encrypt(raw)


def _decrypt(blob: bytes) -> dict[str, Any]:
    if blob.startswith(b"DPAPI1"):
        raw = _dpapi(False, blob[6:])
    elif blob.startswith(b"FERNT1"):
        raw = _fernet().decrypt(blob[6:])
    else:  # pre-header file, assume fernet
        raw = _fernet().decrypt(blob)
    return json.loads(raw.decode("utf-8"))


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if not VAULT_PATH.exists():
        _cache = {}
        return _cache
    try:
        _cache = _decrypt(VAULT_PATH.read_bytes())
    except Exception:
        # A vault we cannot decrypt (different Windows user, lost key file) is
        # not recoverable. Move it aside rather than crash on every launch.
        try:
            VAULT_PATH.replace(VAULT_PATH.with_suffix(".unreadable"))
        except OSError:
            pass
        _cache = {}
    return _cache


def _save() -> None:
    ensure_dirs()
    tmp = VAULT_PATH.with_suffix(".tmp")
    tmp.write_bytes(_encrypt(_cache or {}))
    tmp.replace(VAULT_PATH)


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def put(key: str, value: Any) -> None:
    with _lock:
        _load()[key] = value
        _save()


def delete(key: str) -> None:
    with _lock:
        _load().pop(key, None)
        _save()


def keys() -> list[str]:
    with _lock:
        return sorted(_load().keys())


def has(key: str) -> bool:
    with _lock:
        val = _load().get(key)
        return bool(val)


def masked(key: str) -> str:
    """A safe-to-display version of a stored secret."""
    val = _load().get(key)
    if not val:
        return ""
    if isinstance(val, dict):
        return "signed in"
    s = str(val)
    return f"{s[:3]}…{s[-4:]}" if len(s) > 12 else "•" * len(s)


def resolve_api_key(name: str, env_var: str) -> str | None:
    """Vault first, environment second. Lets you run headless in CI."""
    return get(name) or os.environ.get(env_var) or None


__all__ = ["get", "put", "delete", "keys", "has", "masked", "backend_name",
           "resolve_api_key"]
