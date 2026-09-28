"""Windows idle time across the 32-bit tick boundaries (runs on every platform: pure arithmetic and
a probe with faked user32/kernel32)."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

from nox.sensors.win32 import RealWin32Probe, idle_millis

DAY_MS = 24 * 3600 * 1000
WRAP = 2**32


@pytest.mark.parametrize(
    ("tick", "last_input", "idle"),
    [
        (10_000, 4_000, 6_000),  # the ordinary case
        (2**31 + 5_000, 2**31 - 1_000, 6_000),  # past 24.9 days: signed reading would be < 0
        (25 * DAY_MS, 25 * DAY_MS - 900_000, 900_000),  # 15 min idle at 25 days uptime
        (1_000, WRAP - 2_000, 3_000),  # across the 49.7-day wrap
        (5_000, 5_000, 0),
    ],
)
def test_idle_millis_is_unsigned_32_bit_arithmetic(tick: int, last_input: int, idle: int) -> None:
    assert idle_millis(tick, last_input) == idle


def test_a_tick_read_as_signed_still_gives_the_real_idle_time() -> None:
    signed_tick = (2**31 + 5_000) - WRAP  # what ctypes' default c_int return type produced
    assert idle_millis(signed_tick, 2**31 - 1_000) == 6_000


class _User32:
    def __init__(self, last_input: int) -> None:
        self.last_input = last_input

    def GetLastInputInfo(self, ref: Any) -> int:  # noqa: N802 - Win32 name
        ref._obj.dwTime = self.last_input
        return 1


class _Kernel32:
    def __init__(self, tick: int) -> None:
        self.tick = tick

    def GetTickCount(self) -> int:  # noqa: N802 - Win32 name
        return self.tick


def _probe(tick: int, last_input: int) -> RealWin32Probe:
    probe = object.__new__(RealWin32Probe)

    class _LastInputInfo(ctypes.Structure):
        _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))

    probe._LastInputInfo = _LastInputInfo
    probe._user32 = _User32(last_input)
    probe._kernel32 = _Kernel32(tick)
    return probe


def test_the_probe_reports_idle_after_25_days_of_uptime() -> None:
    """Between 24.9 and 49.7 days the user used to be "active" forever: never idle, never away."""
    signed_tick = (25 * DAY_MS) - WRAP
    probe = _probe(signed_tick, 25 * DAY_MS - 900_000)
    assert probe.idle_seconds() == pytest.approx(900.0)
