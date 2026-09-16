"""Foreground sensor: change-only updates/events, real PrivacyService zone wiring (SP-15), never
persisting window content beyond the state fields/events themselves, kill-switch pause."""

from __future__ import annotations

from nox.core.events import E
from nox.security.privacy import PrivacyService
from nox.sensors.foreground import ForegroundSensor
from tests.unit.fakes import FakeBus, FakeState

from .conftest import FakeWin32Probe


async def test_poll_updates_state_and_emits_only_on_change(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    sensor = ForegroundSensor(probe, bus, state, privacy, poll_interval_s=1.0)

    probe.set_foreground("Notepad", "notepad.exe")
    await sensor.poll()
    assert state.get("user.application") == "notepad.exe"
    assert state.get("user.window_title") == "Notepad"
    assert E.SENSOR_FOREGROUND_CHANGED in bus.names()
    published_after_first = len(bus.published)
    app_updates = [u for u in state.updates if u[0] == "user.application"]
    assert len(app_updates) == 1

    await sensor.poll()  # same window again: no new event, no new state write
    assert len(bus.published) == published_after_first
    assert len([u for u in state.updates if u[0] == "user.application"]) == 1


async def test_zone_entry_and_exit_publish_privacy_zone_changed(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    sensor = ForegroundSensor(probe, bus, state, privacy)

    probe.set_foreground("KeePass - vault.kdbx", "keepass.exe")
    await sensor.poll()
    zone_events = [e.payload for e in bus.published if e.name == E.PRIVACY_ZONE_CHANGED]
    assert zone_events[-1] == {"active": True, "zone": "password_manager"}
    assert privacy.active_zone == "password_manager"

    probe.set_foreground("Notepad", "notepad.exe")
    await sensor.poll()
    zone_events = [e.payload for e in bus.published if e.name == E.PRIVACY_ZONE_CHANGED]
    assert zone_events[-1] == {"active": False, "zone": None}
    assert privacy.active_zone is None


async def test_browser_tab_title_switches_zone_within_same_process(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    """SP-15's hardest case: one process (a browser), tab title flips between zoned/non-zoned."""
    sensor = ForegroundSensor(probe, bus, state, privacy)

    probe.set_foreground("Sparkasse - Mein Konto - Chrome", "chrome.exe")
    await sensor.poll()
    assert privacy.active_zone == "banking"

    probe.set_foreground("nox/repo: EPIC-20 - Chrome", "chrome.exe")
    await sensor.poll()
    assert privacy.active_zone is None

    probe.set_foreground("Discord | #general - Chrome", "chrome.exe")
    await sensor.poll()
    assert privacy.active_zone == "discord"


async def test_zone_event_never_carries_the_window_title(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    sensor = ForegroundSensor(probe, bus, state, privacy)
    probe.set_foreground("Steuererklärung 2025.pdf - Acrobat", "acrobat.exe")
    await sensor.poll()
    assert privacy.active_zone == "personal_documents"
    for event in bus.published:
        if event.name == E.PRIVACY_ZONE_CHANGED:
            assert set(event.payload) == {"active", "zone"}
            assert "Steuer" not in str(event.payload)


async def test_zoned_window_title_is_redacted_before_it_is_ever_written_anywhere(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    """SP-15 finding: `NoxState` is checkpointed to SQLite, so the raw title must never reach
    `user.window_title` (or the `sensor.foreground_changed` payload) even for a zoned window -
    only "an app was open" (the process name) and the zone id may ever persist."""
    sensor = ForegroundSensor(probe, bus, state, privacy)
    probe.set_foreground("KeePass - vault.kdbx", "keepass.exe")

    await sensor.poll()

    assert privacy.active_zone == "password_manager"
    assert state.get("user.window_title") == ""
    assert state.get("user.application") == "keepass.exe"  # app identity still allowed
    foreground_events = [e for e in bus.published if e.name == E.SENSOR_FOREGROUND_CHANGED]
    assert foreground_events[-1].payload == {"process": "keepass.exe", "title": ""}
    for update in state.updates:
        assert "vault.kdbx" not in str(update)
    for event in bus.published:
        assert "vault.kdbx" not in str(event.payload)


async def test_kill_switch_pauses_polling(
    probe: FakeWin32Probe, bus: FakeBus, state: FakeState, privacy: PrivacyService
) -> None:
    sensor = ForegroundSensor(probe, bus, state, privacy, safe_mode=lambda: True)
    probe.set_foreground("Notepad", "notepad.exe")
    result = await sensor.poll()
    assert result is None
    assert sensor.last is None
    assert state.updates == []
    assert bus.published == []
