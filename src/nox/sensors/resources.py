"""Hardware resource sensor: CPU/RAM via `psutil`, GPU/VRAM via `nvidia-smi` when present.

Read-only by construction: this module never calls a fan-speed, voltage or firmware API, and it
owns no control path that could. Adaptive sampling polls faster once CPU crosses
`cpu_high_watermark_pct` and slower otherwise, so the monitoring overhead stays small. GPU/VRAM are
honestly `0.0` with `gpu_available=False` when `nvidia-smi` is absent or fails - never a fabricated
number.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.sensors.history import SensorHistoryStore
from nox.util.aio import poll_loop
from nox.util.proc import find_binary

log = get_logger(__name__)


class PsutilLike(Protocol):
    """The narrow `psutil` surface this sensor needs - tests inject a fake implementing it."""

    def cpu_percent(self, interval: float | None = None) -> float: ...
    def virtual_memory(self) -> Any: ...  # `.used`, `.total` in bytes


class GpuProbe(Protocol):
    """Returns `(util_pct, vram_used_mb, vram_total_mb)` or `None` when no GPU/driver is found."""

    def sample(self) -> tuple[float, float, float] | None: ...


class NvidiaSmiGpuProbe:
    """Real GPU probe via `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`. Absent
    binary or non-zero exit -> `None` (honest "unknown"), never a fabricated reading."""

    def __init__(self, *, timeout_s: float = 3.0) -> None:
        self._timeout_s = timeout_s

    def sample(self) -> tuple[float, float, float] | None:
        """Blocking: run from a worker thread, never directly on the event loop.

        `nvidia-smi` is looked up on every call rather than cached, so a driver installed while
        Nox runs starts being reported without a restart.
        """
        binary = find_binary("nvidia-smi")
        if binary is None:
            return None
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv from PATH lookup, no shell, no input
                [
                    binary,
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("sensors.gpu_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        try:
            util_s, used_s, total_s = out.stdout.strip().splitlines()[0].split(",")
            return float(util_s), float(used_s), float(total_s)
        except (ValueError, IndexError):
            return None


class ResourceSensor:
    def __init__(
        self,
        psutil_mod: PsutilLike,
        state: StateManager,
        *,
        gpu_probe: GpuProbe | None = None,
        history: SensorHistoryStore | None = None,
        poll_interval_s: float = 5.0,
        idle_poll_interval_s: float = 15.0,
        cpu_high_watermark_pct: float = 80.0,
        gpu_enabled: bool = True,
        safe_mode: Callable[[], bool] = lambda: False,
    ) -> None:
        self._psutil = psutil_mod
        self._state = state
        self._gpu = gpu_probe if gpu_enabled else None
        self._history = history
        self._busy_interval = poll_interval_s
        self._idle_interval = idle_poll_interval_s
        self._watermark = cpu_high_watermark_pct
        self._safe_mode = safe_mode
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sensor-resources")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _note_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"

    def _next_interval(self, sample: dict[str, Any] | None) -> float:
        cpu = float(sample["cpu"]) if sample else 0.0
        return self._busy_interval if cpu >= self._watermark else self._idle_interval

    async def _loop(self) -> None:
        await poll_loop(
            self.poll,
            self._next_interval,
            name="sensor-resources",
            on_error=self._note_error,
            on_success=lambda: setattr(self, "last_error", ""),
        )

    async def poll(self) -> dict[str, Any] | None:
        if self._safe_mode():
            return None
        cpu = float(self._psutil.cpu_percent(interval=None))
        mem = self._psutil.virtual_memory()
        ram_mb = float(getattr(mem, "used", 0)) / (1024 * 1024)
        gpu_pct = 0.0
        vram_mb = 0.0
        gpu_available = False
        if self._gpu is not None:
            # `nvidia-smi` takes up to `timeout_s` to answer; on the loop that would freeze IPC
            # and the heartbeats for the whole poll.
            reading = await asyncio.to_thread(self._gpu.sample)
            if reading is not None:
                gpu_pct, vram_mb, _vram_total_mb = reading
                gpu_available = True
        await self._state.update("system.cpu", cpu, reason="sensor.resources")
        await self._state.update("system.ram_mb", ram_mb, reason="sensor.resources")
        await self._state.update("system.gpu", gpu_pct, reason="sensor.resources")
        await self._state.update("system.vram_mb", vram_mb, reason="sensor.resources")
        sample = {
            "cpu": cpu,
            "ram_mb": ram_mb,
            "gpu": gpu_pct,
            "vram_mb": vram_mb,
            "gpu_available": gpu_available,
        }
        if self._history is not None:
            self._history.record("resources", sample)
        return sample
