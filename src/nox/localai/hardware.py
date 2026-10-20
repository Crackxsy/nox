"""Measuring the machine for the model recommendation.

RAM comes from `psutil`, VRAM from the resource sensor's `nvidia-smi` probe - the same readings the
status page shows. An AMD, Intel or Apple GPU has no VRAM reading here, and none is invented: the
recommendation then goes by RAM and says so.

Blocking by nature (`nvidia-smi` is a subprocess): async callers run `detect_hardware` in a thread.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Callable
from pathlib import Path

from nox.ai.local_models import Hardware
from nox.sensors.resources import GpuProbe, NvidiaSmiGpuProbe, PsutilLike

__all__ = ["PI_MODEL_PATH", "detect_hardware", "parse_pi_generation", "read_pi_generation"]

#: Where Linux on a Raspberry Pi names the board ("Raspberry Pi 4 Model B Rev 1.4").
PI_MODEL_PATH = Path("/proc/device-tree/model")

_GIB = 1024**3
_PI = re.compile(r"raspberry pi\s+(zero|\d+)", re.IGNORECASE)


def parse_pi_generation(model: str) -> int | None:
    """`4` for "Raspberry Pi 4 Model B", `0` for a Zero, None for anything that is not a Pi.

    A Compute Module ("Raspberry Pi Compute Module 4") counts as its generation.
    """
    text = model.replace("\x00", " ").replace("Compute Module", "").strip()
    match = _PI.search(text)
    if match is None:
        return None
    value = match.group(1).lower()
    return 0 if value == "zero" else int(value)


def read_pi_generation(path: Path = PI_MODEL_PATH) -> int | None:
    """The Pi generation from the device tree; None off Linux, off a Pi, or when unreadable."""
    try:
        return parse_pi_generation(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _default_psutil() -> PsutilLike:
    import psutil  # noqa: PLC0415 - imported where it is used, like the sensors do

    return psutil


def detect_hardware(
    *,
    psutil_mod: PsutilLike | None = None,
    gpu_probe: GpuProbe | None = None,
    machine: str | None = None,
    pi_generation: Callable[[], int | None] = read_pi_generation,
) -> Hardware:
    """RAM total, VRAM total of the first NVIDIA card (or None), CPU architecture and Pi board."""
    memory = (psutil_mod or _default_psutil()).virtual_memory()
    ram_gb = float(getattr(memory, "total", 0)) / _GIB
    reading = (gpu_probe or NvidiaSmiGpuProbe()).sample()
    vram_gb = None if reading is None or reading[2] <= 0 else reading[2] / 1024.0
    return Hardware(
        ram_gb=round(ram_gb, 1),
        vram_gb=None if vram_gb is None else round(vram_gb, 1),
        machine=machine if machine is not None else platform.machine(),
        pi_generation=pi_generation(),
    )
