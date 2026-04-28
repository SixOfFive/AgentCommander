"""VRAM detection — cross-platform best-effort.

Detection order:
  1. nvidia-smi (Linux/Win/Mac with CUDA)
  2. wmic on Windows (works for non-NVIDIA cards too; reports adapter RAM)
  3. system_profiler-style on macOS for Apple Silicon (unified memory; ~70%
     of total system RAM as practical model ceiling)
  4. Fallback: 0 (autoconfig treats as "unknown" and shows the full catalog)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class VramInfo:
    total_gb: float
    source: str        # "nvidia-smi" | "wmic" | "apple-silicon" | "unknown"
    details: str | None = None


_cached: VramInfo | None = None


def _try_nvidia_smi() -> VramInfo | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(  # noqa: S603
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3.0, check=False,
        )
        if out.returncode != 0:
            return None
        total_mb = 0
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                total_mb += int(line)
        if total_mb > 0:
            return VramInfo(total_gb=round(total_mb / 1024 * 10) / 10, source="nvidia-smi")
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def _try_wmic() -> VramInfo | None:
    if sys.platform != "win32":
        return None
    if not shutil.which("wmic"):
        return None
    try:
        out = subprocess.run(  # noqa: S603
            ["wmic", "path", "Win32_VideoController", "get", "AdapterRAM"],
            capture_output=True, text=True, timeout=3.0, check=False,
        )
        if out.returncode != 0:
            return None
        max_bytes = 0
        for line in out.stdout.splitlines()[1:]:
            line = line.strip()
            if line.isdigit():
                max_bytes = max(max_bytes, int(line))
        if max_bytes > 0:
            return VramInfo(
                total_gb=round(max_bytes / 1024**3 * 10) / 10,
                source="wmic",
                details="reported adapter RAM (may include shared memory on integrated GPUs)",
            )
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def _try_apple_silicon() -> VramInfo | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(  # noqa: S603
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            timeout=3.0, check=False,
        )
        m = re.search(r"\d+", out.stdout or "")
        if not m:
            return None
        total_bytes = int(m.group(0))
        total_gb = total_bytes / 1024**3
        return VramInfo(
            total_gb=round(total_gb * 0.7 * 10) / 10,
            source="apple-silicon",
            details=f"~70% of {total_gb:.1f} GB unified memory",
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def detect_vram(force: bool = False) -> VramInfo:
    """Return total VRAM in GB across detected GPUs. Cached."""
    global _cached
    if _cached and not force:
        return _cached
    _cached = (
        _try_nvidia_smi()
        or _try_wmic()
        or _try_apple_silicon()
        or VramInfo(total_gb=0.0, source="unknown",
                    details="no detection method succeeded")
    )
    # Suppress unused-os import warning; reserved for future extensions.
    _ = os
    return _cached
