"""Runs llama-server for GGUF files AEGIS downloaded itself.

Ollama and LM Studio manage their own processes. For raw GGUFs pulled from
Hugging Face, AEGIS needs its own runner, and llama.cpp's llama-server is the
one that speaks an OpenAI-compatible API out of the box.

AEGIS does not download llama.cpp for you - binaries and hardware backends
(CUDA, Vulkan, plain AVX2) are a decision you should make deliberately. Drop
llama-server.exe into the bin folder, or have it on PATH, and this picks it up.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import BIN_DIR, LOG_DIR, ensure_dirs, settings

_WINDOWS = sys.platform == "win32"
_EXE = "llama-server.exe" if _WINDOWS else "llama-server"


def find_llama_server() -> str | None:
    ensure_dirs()
    local = BIN_DIR / _EXE
    if local.exists():
        return str(local)
    return shutil.which("llama-server")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LlamaServer:
    """A single llama-server process. One model at a time, by design."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.model_path: str = ""
        self.port: int = 0
        self.started_at: float = 0.0
        self.log_path: Path | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict[str, Any]:
        exe = find_llama_server()
        return {
            "available": bool(exe),
            "binary": exe or "",
            "running": self.running,
            "model": Path(self.model_path).name if self.model_path else "",
            "model_path": self.model_path,
            "port": self.port,
            "uptime": round(time.time() - self.started_at, 1) if self.running else 0,
            "log": str(self.log_path) if self.log_path else "",
            "hint": ("" if exe else
                     f"Put {_EXE} in {BIN_DIR} (from the llama.cpp releases page) "
                     f"to run downloaded GGUF files directly."),
        }

    def start(self, model_path: str, *, context: int | None = None,
              gpu_layers: int | None = None,
              threads: int | None = None) -> dict[str, Any]:
        exe = find_llama_server()
        if not exe:
            return {"ok": False, "error":
                    f"llama-server not found. Put {_EXE} in {BIN_DIR}."}
        path = Path(model_path)
        if not path.exists():
            return {"ok": False, "error": f"Model file not found: {model_path}"}

        with self._lock:
            self.stop()
            ensure_dirs()
            port = free_port()
            ctx = int(context or settings.get("fit_context", 4096))
            cmd = [exe, "-m", str(path), "--port", str(port),
                   "--host", "127.0.0.1", "-c", str(ctx)]
            if gpu_layers is not None:
                cmd += ["-ngl", str(int(gpu_layers))]
            if threads:
                cmd += ["-t", str(int(threads))]

            log = LOG_DIR / f"llama-server-{int(time.time())}.log"
            try:
                handle = open(log, "wb")
                self.proc = subprocess.Popen(
                    cmd, stdout=handle, stderr=subprocess.STDOUT,
                    creationflags=0x08000000 if _WINDOWS else 0,
                )
            except Exception as exc:
                return {"ok": False, "error": f"Could not start llama-server: {exc}"}

            self.model_path = str(path)
            self.port = port
            self.started_at = time.time()
            self.log_path = log

        # Wait for it to bind, rather than claiming success optimistically.
        deadline = time.time() + 90
        while time.time() < deadline:
            if not self.running:
                tail = ""
                try:
                    tail = log.read_text("utf-8", "replace")[-800:]
                except OSError:
                    pass
                return {"ok": False,
                        "error": f"llama-server exited during startup.\n{tail}"}
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return {"ok": True, "port": port, "model": path.name,
                            "context": ctx, "log": str(log)}
            except OSError:
                time.sleep(0.5)

        self.stop()
        return {"ok": False, "error": "llama-server did not come up within 90s. "
                                      "The model may be too large for this machine."}

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.model_path = ""
        self.port = 0


server = LlamaServer()
