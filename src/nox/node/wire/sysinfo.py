"""`system.info`: the values of the machine the code runs on.

The same function answers on a paired device and on the core's own machine, so the two report
the same fields. Owns the reading only; which device is asked is `nox.node.hub.router`.
"""

from __future__ import annotations

import asyncio
import platform
import time
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel

from nox import __version__

__all__ = ["system_info"]


async def system_info(_args: BaseModel) -> dict[str, Any]:
    return await asyncio.to_thread(_system_info)


def _system_info() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.home().anchor or "/"))
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
        "cpu_count": psutil.cpu_count() or 0,
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_total_mb": memory.total // (1024 * 1024),
        "memory_used_percent": memory.percent,
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "nox_version": __version__,
    }
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is not None:
        info["battery_percent"] = round(battery.percent)
        info["battery_plugged"] = bool(battery.power_plugged)
    temperatures = getattr(psutil, "sensors_temperatures", None)
    if temperatures is not None:
        try:
            readings = [t.current for entries in temperatures().values() for t in entries]
        except (OSError, RuntimeError):
            readings = []
        if readings:
            info["temperature_c"] = round(max(readings), 1)
    return info
