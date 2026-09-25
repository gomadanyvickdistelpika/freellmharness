"""Hardware probe and the can-it-run calculator.

This is the part that makes AEGIS feel like LM Studio: before you download
anything, it tells you whether the thing will actually run on this machine,
and why.

The maths is deliberately explicit rather than a magic number, because the
answer changes with context length and you should be able to see what moved.

    required = weights + kv_cache(context) + compute_buffer + runtime_overhead

Verdicts
    green   fits in the fast path with headroom
    amber   will run, but it is tight, or partly on the CPU
    red     will not run, or will thrash swap so badly it is pointless

Every number reported here is measured or read from the file. The only
estimates are the compute buffer and the throughput ceiling, and both are
labelled as estimates in what we hand back.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Any

import psutil

from .config import settings
from .gguf import GGUFInfo

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# Names that mean "this GPU has no memory of its own, it is eating system RAM".
_INTEGRATED_PATTERNS = re.compile(
    r"(iris|uhd graphics|hd graphics|intel\(r\) graphics|vega \d+ graphics|"
    r"radeon\(tm\) graphics|radeon graphics|apple m\d|microsoft basic)",
    re.IGNORECASE,
)

# Rough, conservative RAM bandwidth by platform, GB/s. Only used for the
# clearly-labelled throughput ceiling. Override in settings if you know yours.
_DEFAULT_CPU_BANDWIDTH_GBS = 45.0


@dataclass
class GPU:
    name: str
    vram_total: int = 0      # bytes; 0 for integrated
    vram_free: int = 0
    integrated: bool = False
    vendor: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vram_total_gb"] = round(self.vram_total / GB, 2)
        d["vram_free_gb"] = round(self.vram_free / GB, 2)
        return d


@dataclass
class Hardware:
    platform: str
    cpu_name: str
    cpu_cores: int
    cpu_threads: int
    ram_total: int
    ram_available: int
    gpus: list[GPU]
    disk_free: int

    @property
    def best_gpu(self) -> GPU | None:
        discrete = [g for g in self.gpus if not g.integrated and g.vram_total > 0]
        if not discrete:
            return None
        return max(discrete, key=lambda g: g.vram_total)

    @property
    def has_discrete_gpu(self) -> bool:
        return self.best_gpu is not None

    def to_dict(self) -> dict[str, Any]:
        gpu = self.best_gpu
        return {
            "platform": self.platform,
            "cpu_name": self.cpu_name,
            "cpu_cores": self.cpu_cores,
            "cpu_threads": self.cpu_threads,
            "ram_total": self.ram_total,
            "ram_available": self.ram_available,
            "ram_total_gb": round(self.ram_total / GB, 1),
            "ram_available_gb": round(self.ram_available / GB, 1),
            "disk_free_gb": round(self.disk_free / GB, 1),
            "gpus": [g.to_dict() for g in self.gpus],
            "accelerator": gpu.name if gpu else (
                self.gpus[0].name if self.gpus else "CPU only"),
            "has_discrete_gpu": self.has_discrete_gpu,
            "vram_total_gb": round(gpu.vram_total / GB, 1) if gpu else 0.0,
        }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True, "text": True, "timeout": timeout,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(cmd, **kwargs)
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _nvidia_gpus() -> list[GPU]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run(["nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits"])
    gpus: list[GPU] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            gpus.append(GPU(name=parts[0],
                            vram_total=int(float(parts[1])) * MB,
                            vram_free=int(float(parts[2])) * MB,
                            integrated=False, vendor="NVIDIA"))
        except ValueError:
            continue
    return gpus


def _windows_gpus() -> list[GPU]:
    """Adapter names from CIM, dedicated VRAM from the registry.

    Win32_VideoController.AdapterRAM is a uint32 and lies about anything over
    4 GB, so it is only used as a last resort. The registry value
    HardwareInformation.qwMemorySize is the honest one.
    """
    ps = ("Get-CimInstance Win32_VideoController | "
          "ForEach-Object { \"$($_.Name)|$($_.AdapterRAM)\" }")
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 12.0)

    reg_sizes = _windows_registry_vram()
    gpus: list[GPU] = []
    for line in out.strip().splitlines():
        if "|" not in line:
            continue
        name, _, ram_s = line.rpartition("|")
        name = name.strip()
        if not name:
            continue
        integrated = bool(_INTEGRATED_PATTERNS.search(name))
        vram = reg_sizes.get(name, 0)
        if not vram and not integrated:
            try:
                vram = int(ram_s.strip() or 0)
            except ValueError:
                vram = 0
        vendor = ("NVIDIA" if "nvidia" in name.lower() or "geforce" in name.lower()
                  else "AMD" if any(k in name.lower() for k in ("radeon", "amd"))
                  else "Intel" if "intel" in name.lower() else "")
        gpus.append(GPU(name=name, vram_total=0 if integrated else vram,
                        vram_free=0 if integrated else vram,
                        integrated=integrated, vendor=vendor))
    return gpus


def _windows_registry_vram() -> dict[str, int]:
    """Map adapter description -> dedicated VRAM bytes, from the driver keys."""
    sizes: dict[str, int] = {}
    try:
        import winreg  # type: ignore[import-not-found]

        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                        try:
                            size, _ = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")
                        except OSError:
                            continue
                        if isinstance(size, int) and size > 0:
                            sizes[str(desc)] = size
                except OSError:
                    continue
    except Exception:
        pass
    return sizes


def _linux_gpus() -> list[GPU]:
    gpus = _nvidia_gpus()
    if gpus:
        return gpus
    out = _run(["lspci"])
    for line in out.splitlines():
        if "VGA compatible controller" in line or "3D controller" in line:
            name = line.split(":", 2)[-1].strip()
            gpus.append(GPU(name=name,
                            integrated=bool(_INTEGRATED_PATTERNS.search(name))))
    return gpus


def _macos_gpus() -> list[GPU]:
    out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    name = out.strip() or "Apple Silicon"
    # Unified memory: the GPU can address most of system RAM.
    return [GPU(name=name, integrated=True, vendor="Apple")]


def _cpu_name() -> str:
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance Win32_Processor).Name"], 12.0)
        if out.strip():
            return out.strip().splitlines()[0].strip()
    elif sys.platform == "darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out.strip():
            return out.strip()
    else:
        try:
            for line in open("/proc/cpuinfo", encoding="utf-8"):
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return "Unknown CPU"


@functools.lru_cache(maxsize=1)
def _static_probe() -> tuple[str, str, int, int, list[GPU]]:
    if sys.platform == "win32":
        gpus = _nvidia_gpus() or _windows_gpus()
    elif sys.platform == "darwin":
        gpus = _macos_gpus()
    else:
        gpus = _linux_gpus()
    return (sys.platform, _cpu_name(),
            psutil.cpu_count(logical=False) or 0,
            psutil.cpu_count(logical=True) or 0,
            gpus)


def probe(refresh: bool = False) -> Hardware:
    """Current hardware state. Static parts are cached; memory is always live."""
    if refresh:
        _static_probe.cache_clear()
    platform_, cpu, cores, threads, gpus = _static_probe()
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(_disk_target())).free
    except OSError:
        disk = 0

    # Refresh NVIDIA free VRAM each call - it moves as other apps use the GPU.
    if any(g.vendor == "NVIDIA" for g in gpus):
        live = {g.name: g for g in _nvidia_gpus()}
        for g in gpus:
            if g.name in live:
                g.vram_free = live[g.name].vram_free

    return Hardware(platform=platform_, cpu_name=cpu, cpu_cores=cores,
                    cpu_threads=threads, ram_total=vm.total,
                    ram_available=vm.available, gpus=gpus, disk_free=disk)


def _disk_target():
    from .config import MODELS_DIR
    p = MODELS_DIR
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


# ---------------------------------------------------------------------------
# The fit calculator
# ---------------------------------------------------------------------------

GREEN, AMBER, RED = "green", "amber", "red"


def estimate_kv_cache(info: GGUFInfo | None, weights_bytes: int, context: int) -> tuple[int, bool]:
    """KV cache size in bytes, and whether it came from real metadata."""
    if info:
        exact = info.kv_cache_bytes(context)
        if exact:
            return exact, True
    # Fallback: KV cache scales with model size and context. This ratio is
    # derived from measured 7B/13B llama-family models at fp16 cache and is
    # only used when we could not read the header.
    approx = int(weights_bytes * 0.06 * (context / 4096))
    return approx, False


def compute_buffer(weights_bytes: int, context: int) -> int:
    """Activations, logits and scratch space llama.cpp allocates per run."""
    base = 256 * MB
    return base + int(weights_bytes * 0.02) + int(context * 2048)


def throughput_ceiling_tps(required_bytes: int, on_gpu: bool,
                           gpu: GPU | None) -> float | None:
    """Theoretical upper bound on generation speed, tokens/sec.

    Generation is memory-bandwidth bound: every token reads the whole weight
    set once. ceiling = bandwidth / weights. Real throughput lands at roughly
    40-60% of this. Returned as an estimate, flagged as such by the caller.
    """
    if required_bytes <= 0:
        return None
    if on_gpu and gpu and gpu.vendor == "NVIDIA":
        bandwidth_gbs = 400.0          # conservative for a modern discrete card
    else:
        bandwidth_gbs = float(settings.get("mem_bandwidth_gbs",
                                           _DEFAULT_CPU_BANDWIDTH_GBS))
    return round((bandwidth_gbs * GB) / required_bytes, 1)


def assess(weights_bytes: int,
           info: GGUFInfo | None = None,
           context: int | None = None,
           hw: Hardware | None = None) -> dict[str, Any]:
    """Can this machine run this model? Returns the verdict and the workings."""
    hw = hw or probe()
    context = int(context or settings.get("fit_context", 4096))
    if info and info.context_length:
        context = min(context, info.context_length)

    kv_bytes, kv_exact = estimate_kv_cache(info, weights_bytes, context)
    buf_bytes = compute_buffer(weights_bytes, context)
    required = weights_bytes + kv_bytes + buf_bytes

    headroom = int(settings.get("ram_headroom_mb", 2048)) * MB
    gpu = hw.best_gpu
    igpu_as_ram = bool(settings.get("igpu_counts_as_ram", True))

    verdict: str
    headline: str
    detail: str
    offload = "cpu"
    budget: int

    if gpu and gpu.vram_total > 0:
        vram_budget = int((gpu.vram_free or gpu.vram_total) * 0.92)
        budget = vram_budget
        if required <= vram_budget:
            verdict, offload = GREEN, "gpu"
            headline = "Full GPU offload"
            detail = (f"Fits in {gpu.name} VRAM with "
                      f"{_gb(vram_budget - required)} GB to spare.")
        elif weights_bytes * 0.5 <= vram_budget:
            verdict, offload = AMBER, "split"
            layers_pct = int(min(95, (vram_budget / max(required, 1)) * 100))
            headline = f"Partial offload (~{layers_pct}% on GPU)"
            detail = (f"Too big for {_gb(vram_budget)} GB of VRAM, so some layers "
                      f"run on the CPU. It will work; it will be slower.")
            budget = vram_budget + max(0, hw.ram_available - headroom)
            if required > budget:
                verdict = RED
                headline = "Too large for this machine"
                detail = (f"Needs {_gb(required)} GB across GPU and RAM; only "
                          f"{_gb(budget)} GB is available.")
        else:
            verdict, offload = RED, "cpu"
            headline = "Too large for this machine"
            detail = (f"Needs {_gb(required)} GB, VRAM is {_gb(vram_budget)} GB "
                      f"and RAM cannot cover the difference comfortably.")
    else:
        # No usable VRAM: system RAM is the whole budget.
        usable_now = max(0, hw.ram_available - headroom)
        usable_max = max(0, hw.ram_total - headroom)
        budget = usable_now
        igpu = next((g for g in hw.gpus if g.integrated), None)
        note = ""
        if igpu and igpu_as_ram:
            note = f" {igpu.name} shares system memory, so there is no separate VRAM pool."

        if required <= usable_now:
            verdict, offload = GREEN, "cpu"
            headline = "Runs on CPU"
            detail = (f"Needs {_gb(required)} GB, you have {_gb(usable_now)} GB "
                      f"free right now.{note}")
        elif required <= usable_max:
            verdict, offload = AMBER, "cpu"
            headline = "Tight - close some apps first"
            detail = (f"Needs {_gb(required)} GB. Your machine has "
                      f"{_gb(usable_max)} GB usable in total but only "
                      f"{_gb(usable_now)} GB free at the moment.{note}")
            budget = usable_max
        else:
            verdict, offload = RED, "cpu"
            headline = "Will not fit"
            detail = (f"Needs {_gb(required)} GB, this machine tops out at "
                      f"{_gb(usable_max)} GB usable.{note}")
            budget = usable_max

    ceiling = throughput_ceiling_tps(
        weights_bytes if offload != "gpu" else weights_bytes,
        offload == "gpu", gpu)

    return {
        "verdict": verdict,
        "headline": headline,
        "detail": detail,
        "offload": offload,
        "context": context,
        "kv_source": "gguf-header" if kv_exact else "estimated",
        "breakdown": {
            "weights_gb": _gb(weights_bytes),
            "kv_cache_gb": _gb(kv_bytes),
            "overhead_gb": _gb(buf_bytes),
            "required_gb": _gb(required),
            "budget_gb": _gb(budget),
        },
        "required_bytes": required,
        "throughput_ceiling_tps": ceiling,
        "throughput_note": (
            "Theoretical ceiling from memory bandwidth. Expect roughly half "
            "this in practice." if ceiling else None),
        "disk_ok": hw.disk_free > weights_bytes * 1.1,
        "disk_free_gb": _gb(hw.disk_free),
    }


def _gb(n: int | float) -> float:
    return round(max(0, n) / GB, 2)
