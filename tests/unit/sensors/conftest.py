"""Shared fixtures for nox.sensors unit tests: a fake Win32 probe (no ctypes), FakeBus/FakeState
from tests.unit.fakes, and a real PrivacyService built with SP-15's representative zone set."""

from __future__ import annotations

import pytest

from nox.core.state import PrivacyMode
from nox.security.privacy import PrivacyService
from nox.sensors.win32 import ForegroundInfo
from tests.unit.fakes import FakeBus, FakeState


class FakeWin32Probe:
    """Stands in for `RealWin32Probe`; tests drive it directly instead of touching ctypes."""

    def __init__(self) -> None:
        self._foreground = ForegroundInfo("", "", 0)
        self._idle_seconds = 0.0

    def foreground(self) -> ForegroundInfo:
        return self._foreground

    def idle_seconds(self) -> float:
        return self._idle_seconds

    def set_foreground(self, title: str, process: str, pid: int = 1) -> None:
        self._foreground = ForegroundInfo(title, process, pid)

    def set_idle_seconds(self, seconds: float) -> None:
        self._idle_seconds = seconds


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def state() -> FakeState:
    return FakeState()


@pytest.fixture
def probe() -> FakeWin32Probe:
    return FakeWin32Probe()


@pytest.fixture
def privacy(bus: FakeBus) -> PrivacyService:
    # SP-15's four representative zone patterns: password manager, Discord, banking (browser tab
    # title), personal documents (folder/keyword pattern).
    return PrivacyService(
        mode=PrivacyMode.BALANCED,
        zones=["password_manager", "discord", "banking", "personal_documents"],
        bus=bus,
    )
