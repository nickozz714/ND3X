"""
services/local_models/hardware.py

Best-effort hardware probe of the host the backend runs on. Used to rank which
local models will run best→worst. Pure stdlib + optional `nvidia-smi`; every
branch degrades gracefully (returns Nones rather than raising).

`detect_hardware()` accepts injectable `platform_system` and `run` (subprocess
wrapper) so it is unit-testable without touching the real machine.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional


@dataclass
class GPUInfo:
    name: str
    vram_gb: Optional[float] = None
    vendor: str = ""


@dataclass
class HardwareInfo:
    os: str
    arch: str
    cpu_cores: int
    ram_gb: float
    gpus: List[GPUInfo]
    unified_memory: bool          # Apple Silicon: GPU shares system RAM
    disk_free_gb: float
    # Effective budget for model weights (GB).
    usable_model_memory_gb: float

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _default_run(cmd: List[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout
    except Exception:  # noqa: BLE001 — tool missing / not permitted
        return None
    return None


def _total_ram_gb(system: str, run: Callable[[List[str]], Optional[str]]) -> float:
    try:
        if system == "Darwin":
            out = run(["sysctl", "-n", "hw.memsize"])
            if out and out.strip().isdigit():
                return round(int(out.strip()) / (1024 ** 3), 1)
        # POSIX fallback (Linux + Mac)
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
            return round((os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _nvidia_gpus(run: Callable[[List[str]], Optional[str]]) -> List[GPUInfo]:
    out = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    gpus: List[GPUInfo] = []
    if not out:
        return gpus
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                vram = round(float(parts[1]) / 1024, 1)  # MiB -> GiB
            except ValueError:
                vram = None
            gpus.append(GPUInfo(name=parts[0], vram_gb=vram, vendor="nvidia"))
    return gpus


def detect_hardware(
    *,
    platform_system: Optional[str] = None,
    platform_machine: Optional[str] = None,
    run: Callable[[List[str]], Optional[str]] = _default_run,
) -> HardwareInfo:
    system = platform_system or platform.system()
    arch = platform_machine or platform.machine()
    cpu_cores = os.cpu_count() or 1
    ram_gb = _total_ram_gb(system, run)

    is_apple_silicon = system == "Darwin" and arch in ("arm64", "aarch64")
    gpus = _nvidia_gpus(run)
    unified = is_apple_silicon and not gpus
    if unified:
        gpus = [GPUInfo(name="Apple Silicon GPU (unified memory)", vram_gb=ram_gb, vendor="apple")]

    try:
        disk_free_gb = round(shutil.disk_usage("/").free / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        disk_free_gb = 0.0

    # Budget for model weights: discrete GPU -> its VRAM; unified/CPU -> ~70% RAM.
    discrete_vram = next((g.vram_gb for g in gpus if g.vendor == "nvidia" and g.vram_gb), None)
    if discrete_vram:
        usable = round(discrete_vram, 1)
    else:
        usable = round(ram_gb * 0.70, 1)

    return HardwareInfo(
        os=system,
        arch=arch,
        cpu_cores=cpu_cores,
        ram_gb=ram_gb,
        gpus=gpus,
        unified_memory=unified,
        disk_free_gb=disk_free_gb,
        usable_model_memory_gb=usable,
    )
