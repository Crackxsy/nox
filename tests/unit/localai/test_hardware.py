"""Measuring the machine: RAM from psutil, VRAM only from nvidia-smi, a Pi from its device tree."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.localai.hardware import detect_hardware, parse_pi_generation, read_pi_generation


class _Psutil:
    def __init__(self, total_gb: float) -> None:
        self.total = int(total_gb * 1024**3)

    def cpu_percent(self, interval: float | None = None) -> float:
        return 0.0

    def virtual_memory(self) -> Any:
        return SimpleNamespace(total=self.total, used=0)


class _Gpu:
    def __init__(self, reading: tuple[float, float, float] | None) -> None:
        self.reading = reading

    def sample(self) -> tuple[float, float, float] | None:
        return self.reading


def test_an_nvidia_card_gives_a_vram_figure() -> None:
    hw = detect_hardware(
        psutil_mod=_Psutil(32),
        gpu_probe=_Gpu((3.0, 900.0, 6144.0)),
        machine="AMD64",
        pi_generation=lambda: None,
    )
    assert (hw.ram_gb, hw.vram_gb, hw.machine) == (32.0, 6.0, "AMD64")


def test_no_nvidia_smi_means_no_vram_figure_rather_than_zero() -> None:
    hw = detect_hardware(
        psutil_mod=_Psutil(16), gpu_probe=_Gpu(None), machine="x86_64", pi_generation=lambda: None
    )
    assert hw.vram_gb is None


@pytest.mark.parametrize(
    ("model", "generation"),
    [
        ("Raspberry Pi 4 Model B Rev 1.4\x00", 4),
        ("Raspberry Pi 5 Model B Rev 1.0", 5),
        ("Raspberry Pi Zero 2 W Rev 1.0", 0),
        ("Raspberry Pi Compute Module 4 Rev 1.0", 4),
        ("Pine64 RockPro64", None),
    ],
)
def test_pi_generation(model: str, generation: int | None) -> None:
    assert parse_pi_generation(model) == generation


def test_reading_the_device_tree(tmp_path: Path) -> None:
    path = tmp_path / "model"
    path.write_text("Raspberry Pi 4 Model B Rev 1.5\x00", "utf-8")
    assert read_pi_generation(path) == 4
    assert read_pi_generation(tmp_path / "absent") is None
