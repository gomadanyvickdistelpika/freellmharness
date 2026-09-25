"""Minimal GGUF metadata reader.

This is what makes the fit gauge honest instead of a guess. A GGUF file starts
with a key/value metadata block that states the layer count, embedding size and
head counts - exactly the numbers you need to size a KV cache. AEGIS reads it
two ways:

  * from a local file, for models already on disk;
  * over HTTP Range requests, for models on Hugging Face you have not
    downloaded yet - a few KB read instead of a 4 GB download.

If anything about the file is unexpected we return None and the caller falls
back to a size-based estimate. Fail closed, never guess silently.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAGIC = b"GGUF"

# GGUF metadata value types
(U8, I8, U16, I16, U32, I32, F32, BOOL, STRING, ARRAY, U64, I64, F64) = range(13)

_SCALAR = {
    U8: ("<B", 1), I8: ("<b", 1), U16: ("<H", 2), I16: ("<h", 2),
    U32: ("<I", 4), I32: ("<i", 4), F32: ("<f", 4), BOOL: ("<?", 1),
    U64: ("<Q", 8), I64: ("<q", 8), F64: ("<d", 8),
}

# Cap how much of the header we are willing to consume, so a malformed or
# hostile file cannot make us allocate forever.
MAX_HEADER_BYTES = 24 * 1024 * 1024


class ByteReader(Protocol):
    def read(self, offset: int, length: int) -> bytes: ...


class FileReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self, offset: int, length: int) -> bytes:
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)


class HttpRangeReader:
    """Reads byte ranges from a URL, buffering generously to cut round trips."""

    CHUNK = 1 << 20  # 1 MB

    def __init__(self, client, url: str, headers: dict[str, str] | None = None):
        self.client = client            # httpx.Client
        self.url = url
        self.headers = headers or {}
        self._buf = b""
        self._buf_start = -1

    def read(self, offset: int, length: int) -> bytes:
        if (self._buf_start >= 0 and offset >= self._buf_start
                and offset + length <= self._buf_start + len(self._buf)):
            rel = offset - self._buf_start
            return self._buf[rel:rel + length]

        span = max(length, self.CHUNK)
        headers = dict(self.headers)
        headers["Range"] = f"bytes={offset}-{offset + span - 1}"
        resp = self.client.get(self.url, headers=headers,
                               follow_redirects=True, timeout=30.0)
        if resp.status_code not in (200, 206):
            raise OSError(f"range request failed: HTTP {resp.status_code}")
        self._buf = resp.content
        self._buf_start = offset
        return self._buf[:length]


@dataclass
class GGUFInfo:
    architecture: str = ""
    block_count: int = 0          # transformer layers
    embedding_length: int = 0     # n_embd
    head_count: int = 0           # attention heads
    head_count_kv: int = 0        # KV heads (GQA: fewer than head_count)
    context_length: int = 0       # max context the model was trained for
    parameter_count: int = 0      # from metadata when present
    quantisation: str = ""        # e.g. Q4_K_M
    tensor_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def head_dim(self) -> int:
        if self.head_count and self.embedding_length:
            return self.embedding_length // self.head_count
        return 0

    def kv_cache_bytes(self, context: int, bytes_per_element: int = 2) -> int:
        """Size of the K and V caches at a given context length.

        2 (K and V) x layers x context x kv_heads x head_dim x element size.
        Defaults to fp16 cache, which is what llama.cpp uses unless you ask
        for a quantised cache.
        """
        kv_heads = self.head_count_kv or self.head_count
        if not (self.block_count and kv_heads and self.head_dim):
            return 0
        return (2 * self.block_count * max(context, 1) * kv_heads
                * self.head_dim * bytes_per_element)


def _read_string(r: ByteReader, off: int) -> tuple[str, int]:
    (n,) = struct.unpack("<Q", r.read(off, 8))
    off += 8
    if n > 1 << 20:
        raise ValueError("implausible string length in GGUF header")
    raw = r.read(off, n)
    return raw.decode("utf-8", "replace"), off + n


def _read_value(r: ByteReader, off: int, vtype: int) -> tuple[Any, int]:
    if vtype in _SCALAR:
        fmt, size = _SCALAR[vtype]
        (val,) = struct.unpack(fmt, r.read(off, size))
        return val, off + size
    if vtype == STRING:
        return _read_string(r, off)
    if vtype == ARRAY:
        (itype,) = struct.unpack("<I", r.read(off, 4))
        (count,) = struct.unpack("<Q", r.read(off + 4, 8))
        off += 12
        if count > 1 << 24:
            raise ValueError("implausible array length in GGUF header")
        # We never need array *contents* for sizing, and token lists are huge.
        # Skip fixed-width arrays by arithmetic; walk variable-width ones.
        if itype in _SCALAR:
            return [], off + _SCALAR[itype][1] * count
        if itype == STRING:
            for _ in range(count):
                _, off = _read_string(r, off)
            return [], off
        raise ValueError(f"unsupported array element type {itype}")
    raise ValueError(f"unsupported GGUF value type {vtype}")


# File-type id -> human label. Covers what you will actually meet on the Hub.
_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
    15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS",
    20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S",
    25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M",
    30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0",
}


def read_gguf(reader: ByteReader) -> GGUFInfo | None:
    """Parse a GGUF header. Returns None if this is not readable GGUF."""
    try:
        head = reader.read(0, 24)
        if len(head) < 24 or head[:4] != MAGIC:
            return None
        version, tensor_count, kv_count = struct.unpack("<IQQ", head[4:24])
        if version not in (2, 3) or kv_count > 100_000:
            return None

        off = 24
        meta: dict[str, Any] = {}
        for _ in range(kv_count):
            if off > MAX_HEADER_BYTES:
                return None
            key, off = _read_string(reader, off)
            (vtype,) = struct.unpack("<I", reader.read(off, 4))
            off += 4
            value, off = _read_value(reader, off, vtype)
            if not isinstance(value, list):   # arrays are skipped, not stored
                meta[key] = value

        arch = str(meta.get("general.architecture", "") or "")
        info = GGUFInfo(
            architecture=arch,
            block_count=int(meta.get(f"{arch}.block_count", 0) or 0),
            embedding_length=int(meta.get(f"{arch}.embedding_length", 0) or 0),
            head_count=int(meta.get(f"{arch}.attention.head_count", 0) or 0),
            head_count_kv=int(meta.get(f"{arch}.attention.head_count_kv", 0) or 0),
            context_length=int(meta.get(f"{arch}.context_length", 0) or 0),
            parameter_count=int(meta.get("general.parameter_count", 0) or 0),
            quantisation=_FILE_TYPES.get(int(meta.get("general.file_type", -1) or -1), ""),
            tensor_count=int(tensor_count),
            metadata=meta,
        )
        return info
    except Exception:
        return None


def read_local(path: str | Path) -> GGUFInfo | None:
    try:
        return read_gguf(FileReader(path))
    except OSError:
        return None


def read_remote(client, url: str, headers: dict[str, str] | None = None) -> GGUFInfo | None:
    try:
        return read_gguf(HttpRangeReader(client, url, headers))
    except Exception:
        return None


def quant_from_filename(name: str) -> str:
    """Last-resort quantisation guess from the filename (TheBloke convention)."""
    upper = name.upper()
    for tag in ("IQ1_S", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ2_M",
                "IQ3_XXS", "IQ3_XS", "IQ3_S", "IQ3_M", "IQ4_XS", "IQ4_NL",
                "Q2_K_S", "Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L", "Q4_K_S",
                "Q4_K_M", "Q5_K_S", "Q5_K_M", "Q6_K", "Q8_0", "Q4_0", "Q4_1",
                "Q5_0", "Q5_1", "BF16", "F16", "F32"):
        if tag in upper:
            return tag
    return ""
