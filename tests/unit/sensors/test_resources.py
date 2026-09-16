"""Resource sensor: CPU/RAM via a fake psutil-like object, GPU/VRAM via a fake probe (honest
0.0/unavailable without one), kill-switch pause."""

from __future__ import annotations

from nox.sensors.resources import ResourceSensor
from tests.unit.fakes import FakeState


class FakeVirtualMemory:
    def __init__(self, used_bytes: float) -> None:
        self.used = used_bytes
        self.total = used_bytes * 2


class FakePsutil:
    def __init__(self, cpu_pct: float, used_bytes: float) -> None:
        self._cpu = cpu_pct
        self._mem = FakeVirtualMemory(used_bytes)

    def cpu_percent(self, interval: float | None = None) -> float:
        return self._cpu

    def virtual_memory(self) -> FakeVirtualMemory:
        return self._mem


class FakeGpuProbe:
    def __init__(self, reading: tuple[float, float, float] | None) -> None:
        self._reading = reading

    def sample(self) -> tuple[float, float, float] | None:
        return self._reading


async def test_samples_cpu_ram_gpu_vram_into_state(state: FakeState) -> None:
    ps = FakePsutil(cpu_pct=17.5, used_bytes=512 * 1024 * 1024)
    gpu = FakeGpuProbe((42.0, 2048.0, 8192.0))
    sensor = ResourceSensor(
        ps, state, gpu_probe=gpu, poll_interval_s=5.0, idle_poll_interval_s=15.0
    )

    sample = await sensor.poll()

    assert sample == {
        "cpu": 17.5,
        "ram_mb": 512.0,
        "gpu": 42.0,
        "vram_mb": 2048.0,
        "gpu_available": True,
    }
    assert state.get("system.cpu") == 17.5
    assert state.get("system.ram_mb") == 512.0
    assert state.get("system.gpu") == 42.0
    assert state.get("system.vram_mb") == 2048.0


async def test_no_gpu_probe_is_honest_zero_not_fabricated(state: FakeState) -> None:
    ps = FakePsutil(cpu_pct=5.0, used_bytes=1024 * 1024)
    sensor = ResourceSensor(ps, state, gpu_probe=None)

    sample = await sensor.poll()

    assert sample is not None
    assert sample["gpu"] == 0.0
    assert sample["vram_mb"] == 0.0
    assert sample["gpu_available"] is False


async def test_gpu_probe_failure_falls_back_to_honest_unavailable(state: FakeState) -> None:
    ps = FakePsutil(cpu_pct=5.0, used_bytes=1024 * 1024)
    gpu = FakeGpuProbe(None)  # nvidia-smi absent/failed
    sensor = ResourceSensor(ps, state, gpu_probe=gpu)

    sample = await sensor.poll()

    assert sample is not None
    assert sample["gpu"] == 0.0 and sample["gpu_available"] is False


async def test_kill_switch_pauses_sampling(state: FakeState) -> None:
    ps = FakePsutil(cpu_pct=99.0, used_bytes=1024 * 1024)
    sensor = ResourceSensor(ps, state, safe_mode=lambda: True)

    result = await sensor.poll()

    assert result is None
    assert state.updates == []
