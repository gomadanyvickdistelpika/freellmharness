"""Download manager: Hugging Face GGUF files and Ollama pulls, with progress.

Both sources are normalised to the same Job shape so the UI has one progress
bar to render. HF downloads resume from a .part file if you cancel and retry;
Ollama handles its own resume internally.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import HF_HOST, MODELS_DIR, OLLAMA_HOST, ensure_dirs

CHUNK = 1 << 20  # 1 MB


@dataclass
class Job:
    id: str
    ref: str                    # repo/path, or ollama model name
    source: str                 # "hf" | "ollama"
    label: str
    status: str = "queued"      # queued|downloading|verifying|done|error|cancelled
    total: int = 0
    done: int = 0
    speed: float = 0.0          # bytes/sec, rolling
    message: str = ""
    error: str = ""
    path: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def percent(self) -> float:
        return round(self.done / self.total * 100, 1) if self.total else 0.0

    @property
    def eta_seconds(self) -> int | None:
        if self.speed <= 0 or not self.total or self.done >= self.total:
            return None
        return int((self.total - self.done) / self.speed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ref": self.ref, "source": self.source,
            "label": self.label, "status": self.status,
            "total": self.total, "done": self.done,
            "percent": self.percent, "speed": round(self.speed),
            "eta_seconds": self.eta_seconds, "message": self.message,
            "error": self.error, "path": self.path,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class DownloadManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- public -----------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        return [j.to_dict() for j in
                sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in ("done", "error", "cancelled"):
            return False
        job._cancel.set()
        return True

    def clear_finished(self) -> int:
        gone = [k for k, j in self.jobs.items()
                if j.status in ("done", "error", "cancelled")]
        for k in gone:
            self.jobs.pop(k, None)
            self._tasks.pop(k, None)
        return len(gone)

    def start_hf(self, ref: str, token: str | None = None) -> Job:
        """ref is 'owner/repo/path/to/file.gguf'."""
        job = Job(id=uuid.uuid4().hex[:12], ref=ref, source="hf",
                  label=ref.rsplit("/", 1)[-1])
        self.jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run_hf(job, token))
        return job

    def start_ollama(self, model: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], ref=model, source="ollama",
                  label=model)
        self.jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run_ollama(job))
        return job

    # -- workers ----------------------------------------------------------

    async def _run_hf(self, job: Job, token: str | None) -> None:
        ensure_dirs()
        parts = job.ref.split("/")
        if len(parts) < 3:
            job.status, job.error = "error", "Expected owner/repo/file.gguf"
            return
        repo = "/".join(parts[:2])
        rel = "/".join(parts[2:])
        filename = rel.rsplit("/", 1)[-1]
        dest = MODELS_DIR / filename
        part = dest.with_suffix(dest.suffix + ".part")
        url = f"{HF_HOST}/{repo}/resolve/main/{rel}"

        headers = {"User-Agent": "aegis/0.1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resume_from = part.stat().st_size if part.exists() else 0
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
            job.message = f"Resuming from {resume_from / 1e9:.2f} GB"

        job.status = "downloading"
        job.done = resume_from
        window: list[tuple[float, int]] = []

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers,
                                         timeout=httpx.Timeout(30.0, read=120.0)) as resp:
                    if resp.status_code == 416:         # already complete
                        resume_from = part.stat().st_size
                        job.total = job.done = resume_from
                    elif resp.status_code == 401:
                        raise RuntimeError(
                            "Hugging Face refused the download (401). This repo is "
                            "gated - sign in on the Providers tab and accept the "
                            "model licence on huggingface.co first.")
                    elif resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code} from Hugging Face")
                    else:
                        length = int(resp.headers.get("content-length") or 0)
                        job.total = resume_from + length if resp.status_code == 206 else length

                        mode = "ab" if resp.status_code == 206 and resume_from else "wb"
                        if mode == "wb":
                            job.done = 0
                        with open(part, mode) as fh:
                            async for chunk in resp.aiter_bytes(CHUNK):
                                if job._cancel.is_set():
                                    job.status = "cancelled"
                                    job.message = "Cancelled. Partial file kept for resume."
                                    job.finished = time.time()
                                    return
                                fh.write(chunk)
                                job.done += len(chunk)

                                now = time.time()
                                window.append((now, len(chunk)))
                                cutoff = now - 5.0
                                while window and window[0][0] < cutoff:
                                    window.pop(0)
                                span = now - window[0][0] if window else 0
                                if span > 0.5:
                                    job.speed = sum(b for _, b in window) / span

            job.status = "verifying"
            job.message = "Checking the file"
            if job.total and part.stat().st_size < job.total:
                raise RuntimeError(
                    f"Incomplete: got {part.stat().st_size} of {job.total} bytes")

            from .gguf import read_local
            part.replace(dest)
            info = read_local(dest)
            if info is None:
                job.message = ("Downloaded, but the GGUF header did not parse. "
                               "The file may be corrupt or not a GGUF.")
            else:
                job.message = (f"{info.architecture} · {info.block_count} layers"
                               f"{' · ' + info.quantisation if info.quantisation else ''}")
            job.path = str(dest)
            job.status = "done"
            job.finished = time.time()

        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished = time.time()

    async def _run_ollama(self, job: Job) -> None:
        job.status = "downloading"
        payload = {"model": job.ref, "stream": True}
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", f"{OLLAMA_HOST}/api/pull",
                                         json=payload,
                                         timeout=httpx.Timeout(30.0, read=None)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"Ollama returned HTTP {resp.status_code}. "
                            f"Is Ollama running?")
                    last = 0
                    last_t = time.time()
                    async for line in resp.aiter_lines():
                        if job._cancel.is_set():
                            job.status = "cancelled"
                            job.message = "Cancelled."
                            job.finished = time.time()
                            return
                        line = line.strip()
                        if not line:
                            continue
                        import json as _json
                        try:
                            evt = _json.loads(line)
                        except ValueError:
                            continue
                        if err := evt.get("error"):
                            raise RuntimeError(err)
                        job.message = evt.get("status") or job.message
                        if evt.get("total"):
                            job.total = int(evt["total"])
                        if evt.get("completed") is not None:
                            job.done = int(evt["completed"])
                            now = time.time()
                            if now - last_t > 1.0:
                                job.speed = max(0, (job.done - last) / (now - last_t))
                                last, last_t = job.done, now
            job.status = "done"
            job.done = job.total or job.done
            job.message = "Pulled into Ollama"
            job.finished = time.time()
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished = time.time()


manager = DownloadManager()


def delete_local_model(path: str) -> tuple[bool, str]:
    """Remove a GGUF AEGIS downloaded. Refuses anything outside the models dir."""
    p = Path(path).resolve()
    try:
        p.relative_to(MODELS_DIR.resolve())
    except ValueError:
        return False, "Refusing to delete a file outside the AEGIS models folder."
    if not p.exists():
        return False, "File is already gone."
    try:
        p.unlink()
        return True, f"Deleted {p.name}"
    except OSError as exc:
        return False, str(exc)
