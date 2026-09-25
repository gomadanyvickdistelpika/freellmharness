"""One model list, three sources.

  installed/ollama    models already pulled into Ollama       (live API)
  installed/lmstudio  models LM Studio can serve              (live API)
  installed/aegis     GGUF files AEGIS downloaded itself      (disk)
  available/ollama    a curated shortlist you can pull        (static)
  available/hf        anything on Hugging Face with a GGUF    (live search)

Every entry is normalised to the same shape so the UI renders one grid, and
every entry carries enough size information for hardware.assess() to give a
verdict before you download a byte.

Sizes for un-pulled Ollama models are *estimates* computed from the parameter
count, and are flagged as such. Ollama has no public catalogue API, so the
alternative would be a hardcoded table that silently goes stale.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx

from .config import HF_API, LMSTUDIO_HOST, MODELS_DIR, OLLAMA_HOST
from .gguf import quant_from_filename

GB = 1024 ** 3

# Bits per weight for Ollama's default quantisation (Q4_K_M), including the
# fp16 embedding and output layers that are not quantised as aggressively.
# Calibrated against published Q4_K_M file sizes for Llama and Qwen, which land
# at 4.88-4.93 bits per parameter once those unquantised layers are counted.
_DEFAULT_BPW = 4.9


@dataclass
class ModelEntry:
    id: str                      # canonical id, e.g. "ollama:llama3.1:8b"
    name: str                    # display name
    source: str                  # ollama | lmstudio | aegis | hf
    installed: bool = False
    size_bytes: int = 0
    size_estimated: bool = False
    params: str = ""             # "8B"
    quant: str = ""              # "Q4_K_M"
    family: str = ""
    description: str = ""
    path: str = ""               # local file, when installed
    download_ref: str = ""       # what to pass to the downloader
    context_length: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["size_gb"] = round(self.size_bytes / GB, 2) if self.size_bytes else 0.0
        return d


def params_to_bytes(params_b: float, bpw: float = _DEFAULT_BPW) -> int:
    """Parameter count in billions -> approximate file size in bytes."""
    return int(params_b * 1e9 * bpw / 8)


_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([bBmM])\b")


def parse_params(text: str) -> float:
    """Pull a parameter count out of a model name. Returns billions, 0 if none."""
    for match in _PARAM_RE.finditer(text or ""):
        value, unit = float(match.group(1)), match.group(2).lower()
        billions = value / 1000 if unit == "m" else value
        if 0.05 <= billions <= 2000:
            return billions
    return 0.0


# ---------------------------------------------------------------------------
# Curated pull list for Ollama. Names only; sizes are computed, not asserted.
# ---------------------------------------------------------------------------

OLLAMA_SHORTLIST: list[dict[str, Any]] = [
    {"name": "qwen2.5:0.5b", "params": 0.5, "family": "Qwen2.5", "ctx": 32768,
     "desc": "Tiny. Good for testing the harness end to end."},
    {"name": "llama3.2:1b", "params": 1.24, "family": "Llama 3.2", "ctx": 131072,
     "desc": "Fast on any CPU. Summarising and simple extraction."},
    {"name": "qwen2.5:1.5b", "params": 1.54, "family": "Qwen2.5", "ctx": 32768,
     "desc": "Small all-rounder, noticeably better than 1B at instructions."},
    {"name": "llama3.2:3b", "params": 3.21, "family": "Llama 3.2", "ctx": 131072,
     "desc": "The realistic sweet spot for a 16 GB laptop with no GPU."},
    {"name": "qwen2.5:3b", "params": 3.09, "family": "Qwen2.5", "ctx": 32768,
     "desc": "Strong small model for structured output."},
    {"name": "phi3.5:3.8b", "params": 3.8, "family": "Phi-3.5", "ctx": 131072,
     "desc": "Reasoning-heavy for its size. Long context."},
    {"name": "gemma2:2b", "params": 2.6, "family": "Gemma 2", "ctx": 8192,
     "desc": "Compact, clean prose, short context."},
    {"name": "qwen2.5-coder:1.5b", "params": 1.54, "family": "Qwen2.5 Coder",
     "ctx": 32768, "desc": "Code completion that fits anywhere."},
    {"name": "qwen2.5-coder:7b", "params": 7.62, "family": "Qwen2.5 Coder",
     "ctx": 32768, "desc": "Genuinely useful local coding model. Wants a GPU."},
    {"name": "mistral:7b", "params": 7.25, "family": "Mistral", "ctx": 32768,
     "desc": "Old reliable. Solid general instruction following."},
    {"name": "llama3.1:8b", "params": 8.03, "family": "Llama 3.1", "ctx": 131072,
     "desc": "The default 8B. Tight on 16 GB RAM, comfortable on a GPU."},
    {"name": "qwen2.5:7b", "params": 7.62, "family": "Qwen2.5", "ctx": 32768,
     "desc": "Best-in-class 7B for tool calling and JSON."},
    {"name": "gemma2:9b", "params": 9.24, "family": "Gemma 2", "ctx": 8192,
     "desc": "Strong writing. Needs a GPU to be pleasant."},
    {"name": "qwen2.5:14b", "params": 14.8, "family": "Qwen2.5", "ctx": 32768,
     "desc": "Discrete GPU territory. 12 GB VRAM or better."},
    {"name": "nomic-embed-text", "params": 0.137, "family": "Nomic",
     "ctx": 8192, "desc": "Embeddings, not chat. For local RAG."},
]


# ---------------------------------------------------------------------------
# Live sources
# ---------------------------------------------------------------------------

async def ollama_installed(client: httpx.AsyncClient) -> list[ModelEntry]:
    try:
        r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=4.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out: list[ModelEntry] = []
    for m in data.get("models", []):
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        details = m.get("details") or {}
        param_str = details.get("parameter_size") or ""
        out.append(ModelEntry(
            id=f"ollama:{name}",
            name=name,
            source="ollama",
            installed=True,
            size_bytes=int(m.get("size") or 0),
            params=param_str,
            quant=details.get("quantization_level") or "",
            family=details.get("family") or "",
            download_ref=name,
            tags=["installed"],
        ))
    return out


async def lmstudio_installed(client: httpx.AsyncClient) -> list[ModelEntry]:
    try:
        r = await client.get(f"{LMSTUDIO_HOST}/v1/models", timeout=4.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out: list[ModelEntry] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        params = parse_params(mid)
        out.append(ModelEntry(
            id=f"lmstudio:{mid}",
            name=mid,
            source="lmstudio",
            installed=True,
            size_bytes=params_to_bytes(params) if params else 0,
            size_estimated=bool(params),
            params=f"{params:g}B" if params else "",
            quant=quant_from_filename(mid),
            download_ref=mid,
            tags=["installed"],
        ))
    return out


def aegis_installed() -> list[ModelEntry]:
    out: list[ModelEntry] = []
    if not MODELS_DIR.exists():
        return out
    for path in sorted(MODELS_DIR.rglob("*.gguf")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        params = parse_params(path.stem)
        out.append(ModelEntry(
            id=f"aegis:{path.name}",
            name=path.stem,
            source="aegis",
            installed=True,
            size_bytes=size,
            params=f"{params:g}B" if params else "",
            quant=quant_from_filename(path.name),
            path=str(path),
            tags=["installed", "gguf"],
        ))
    return out


def ollama_available(installed_names: set[str]) -> list[ModelEntry]:
    out: list[ModelEntry] = []
    for spec in OLLAMA_SHORTLIST:
        name = spec["name"]
        if name in installed_names:
            continue
        params = float(spec["params"])
        out.append(ModelEntry(
            id=f"ollama:{name}",
            name=name,
            source="ollama",
            installed=False,
            size_bytes=params_to_bytes(params),
            size_estimated=True,
            params=f"{params:g}B",
            quant="Q4_K_M",
            family=spec.get("family", ""),
            description=spec.get("desc", ""),
            download_ref=name,
            context_length=int(spec.get("ctx") or 0),
            tags=["pullable"],
        ))
    return out


async def hf_search(client: httpx.AsyncClient, query: str,
                    limit: int = 20, token: str | None = None) -> list[ModelEntry]:
    """Search Hugging Face for GGUF repos matching a query."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {
        "search": query,
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": str(limit),
    }
    try:
        r = await client.get(f"{HF_API}/models", params=params,
                             headers=headers, timeout=15.0)
        r.raise_for_status()
        repos = r.json()
    except Exception:
        return []

    out: list[ModelEntry] = []
    for repo in repos:
        rid = repo.get("id") or repo.get("modelId") or ""
        if not rid:
            continue
        params_b = parse_params(rid)
        out.append(ModelEntry(
            id=f"hf:{rid}",
            name=rid,
            source="hf",
            installed=False,
            size_bytes=0,               # filled in when you expand the repo
            params=f"{params_b:g}B" if params_b else "",
            family=rid.split("/")[0],
            description=f"{repo.get('downloads', 0):,} downloads",
            download_ref=rid,
            tags=list(repo.get("tags") or [])[:6],
        ))
    return out


async def hf_files(client: httpx.AsyncClient, repo_id: str,
                   token: str | None = None) -> list[ModelEntry]:
    """List the GGUF files in a repo, with real byte sizes."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = await client.get(f"{HF_API}/models/{repo_id}/tree/main",
                             params={"recursive": "true"},
                             headers=headers, timeout=20.0)
        r.raise_for_status()
        tree = r.json()
    except Exception:
        return []

    out: list[ModelEntry] = []
    for node in tree:
        path = node.get("path") or ""
        if node.get("type") != "file" or not path.lower().endswith(".gguf"):
            continue
        size = int(node.get("size") or (node.get("lfs") or {}).get("size") or 0)
        params_b = parse_params(path) or parse_params(repo_id)
        out.append(ModelEntry(
            id=f"hf:{repo_id}/{path}",
            name=path.rsplit("/", 1)[-1],
            source="hf",
            installed=(MODELS_DIR / path.rsplit("/", 1)[-1]).exists(),
            size_bytes=size,
            params=f"{params_b:g}B" if params_b else "",
            quant=quant_from_filename(path),
            family=repo_id,
            download_ref=f"{repo_id}/{path}",
            tags=["gguf"],
        ))
    out.sort(key=lambda e: e.size_bytes)
    return out


async def installed_all(client: httpx.AsyncClient) -> list[ModelEntry]:
    """Everything already on this machine, from all three local sources."""
    ollama, lmstudio = await asyncio.gather(
        ollama_installed(client), lmstudio_installed(client))
    return [*ollama, *lmstudio, *aegis_installed()]


async def browse(client: httpx.AsyncClient) -> list[ModelEntry]:
    """Installed first, then the curated pull list. The default Models view."""
    installed = await installed_all(client)
    names = {e.name for e in installed if e.source == "ollama"}
    return [*installed, *ollama_available(names)]
