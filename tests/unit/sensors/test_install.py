"""Privacy zones do not depend on the awareness sensors: `sensors.enabled: false` still watches the
foreground window for zones, and only `privacy.zones_enabled: false` turns that off - visibly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import nox.sensors.install as sensors_install
from nox.core.config import NoxConfig
from nox.core.events import E, HealthStatus
from nox.core.health import Check
from nox.security.privacy import UNOBSERVABLE_ZONE, PrivacyService
from nox.sensors.probe import ProbeSelection
from tests.unit.fakes import FakeBus, FakeState

from .conftest import FakeWin32Probe


class _Registry:
    def __init__(self) -> None:
        self.names: list[str] = []

    def register(self, spec: Any) -> None:
        self.names.append(spec.name)


class _Health:
    def __init__(self) -> None:
        self.checks: dict[str, Check] = {}

    def add_check(self, check: Check) -> None:
        self.checks[check.name] = check


def _core(
    bus: FakeBus,
    state: FakeState,
    *,
    sensors_enabled: bool,
    zones_enabled: bool = True,
) -> SimpleNamespace:
    config = NoxConfig.model_validate(
        {
            "sensors": {"enabled": sensors_enabled},
            "privacy": {"zones": ["banking"], "zones_enabled": zones_enabled},
        }
    )
    privacy = PrivacyService.from_config(config.privacy, bus=bus)
    return SimpleNamespace(
        config=config,
        bus=bus,
        state=state,
        security=SimpleNamespace(
            privacy=privacy, killswitch=SimpleNamespace(is_engaged=lambda: False)
        ),
        tool_registry=_Registry(),
        health=_Health(),
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> FakeWin32Probe:
    fake = FakeWin32Probe()
    monkeypatch.setattr(sensors_install, "select_probe", lambda: ProbeSelection(fake))
    return fake


async def test_sensors_off_still_enter_the_unobservable_zone(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState
) -> None:
    core = _core(bus, state, sensors_enabled=False)
    probe.set_unobservable("Wayland does not expose the active window")

    runtime = sensors_install.install(core)  # type: ignore[arg-type]
    try:
        assert runtime.foreground is not None and runtime.foreground.zones_only
        assert (runtime.idle, runtime.resources, runtime.game) == (None, None, None)
        await runtime.foreground.poll()
        assert core.security.privacy.active_zone == UNOBSERVABLE_ZONE
    finally:
        await runtime.stop()


async def test_sensors_off_still_close_a_real_zone_but_record_nothing(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState
) -> None:
    core = _core(bus, state, sensors_enabled=False)
    probe.set_foreground("Sparkasse Online-Banking - Browser", "browser.exe")

    runtime = sensors_install.install(core)  # type: ignore[arg-type]
    try:
        assert runtime.foreground is not None
        await runtime.foreground.poll()
        assert core.security.privacy.active_zone == "banking"
        assert not core.security.privacy.allows_capture("screen")
        # Zones only: no window state, no foreground event, no history.
        assert state.updates == []
        assert E.SENSOR_FOREGROUND_CHANGED not in bus.names()
        assert runtime.history.recent("foreground") == []
        status, reason = await core.health.checks["sensors"].probe()
        assert status is HealthStatus.AVAILABLE and "privacy-zone sensing only" in reason
    finally:
        await runtime.stop()


async def test_only_the_privacy_setting_switches_zones_off_and_health_says_so(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState
) -> None:
    core = _core(bus, state, sensors_enabled=False, zones_enabled=False)
    probe.set_unobservable("Wayland does not expose the active window")

    runtime = sensors_install.install(core)  # type: ignore[arg-type]
    assert runtime.sensors() == []
    assert core.security.privacy.active_zone is None
    status, reason = await core.health.checks["sensors"].probe()
    assert status is HealthStatus.LIMITED
    assert "privacy.zones_enabled: false" in reason and "sensors.enabled: false" in reason


async def test_zones_off_with_sensors_on_is_reported_and_enters_no_zone(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState
) -> None:
    core = _core(bus, state, sensors_enabled=True, zones_enabled=False)
    probe.set_foreground("Sparkasse Online-Banking - Browser", "browser.exe")

    runtime = sensors_install.install(core)  # type: ignore[arg-type]
    try:
        assert runtime.foreground is not None and not runtime.foreground.zones_only
        await runtime.foreground.poll()
        assert core.security.privacy.active_zone is None
        status, reason = await core.health.checks["sensors"].probe()
        assert status is HealthStatus.LIMITED and "privacy.zones_enabled: false" in reason
    finally:
        await runtime.stop()
