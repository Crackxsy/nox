"""PC Awareness Sensors integration (SP-15/ST-20-04): boots the real `NoxCore` headless, wires the
sensors with `nox.sensors.install.install(core)`, then swaps the real `RealWin32Probe` the
foreground sensor got wired with for a fake one (no ctypes/real desktop dependency in a test) and
drives one poll cycle with a window title matching a configured privacy zone. Asserts the zone
reaches the real `PrivacyService` and, end to end through the real bus, the real `PetService`
(`privacy.zone_changed` -> `PetFunctional.PRIVACY`) - no mocking of the wiring itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E
from nox.core.state import PetFunctional
from nox.sensors.install import install
from nox.sensors.win32 import ForegroundInfo
from tests._ports import free_port_base


class FakeWin32Probe:
    def __init__(self) -> None:
        self._foreground = ForegroundInfo("", "", 0)

    def foreground(self) -> ForegroundInfo:
        return self._foreground

    def idle_seconds(self) -> float:
        return 0.0

    def set_foreground(self, title: str, process: str, pid: int = 1) -> None:
        self._foreground = ForegroundInfo(title, process, pid)


def _config(tmp_path: Path):
    base = free_port_base()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {
            "mode": "balanced",
            "zones": ["password_manager", "discord", "banking", "personal_documents"],
        },
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "sensors": {"enabled": True},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await core.start()
    install(core)
    try:
        yield core
    finally:
        await core.sensors.stop()
        await core.stop()


async def test_install_registers_sensors_status_read_tool(core: NoxCore) -> None:
    assert core.tool_registry.get("sensors.status.read") is not None


async def test_zoned_foreground_window_reaches_privacy_and_pet(core: NoxCore) -> None:
    sensor = core.sensors.foreground
    # This test host is Windows (ENGINEERING.md), so install() built a RealWin32Probe.
    assert sensor is not None

    # Swap the ctypes probe for a deterministic fake; the rest of the wiring stays real.
    fake = FakeWin32Probe()
    sensor._probe = fake

    fake.set_foreground("KeePass - vault.kdbx", "keepass.exe")
    zone_event = None

    async def _capture(ev: object) -> None:
        nonlocal zone_event
        zone_event = ev

    unsub = core.bus.subscribe(E.PRIVACY_ZONE_CHANGED, _capture)
    try:
        await sensor.poll()
    finally:
        unsub()

    assert zone_event is not None
    assert zone_event.payload == {"active": True, "zone": "password_manager"}
    assert core.security.privacy.active_zone == "password_manager"
    assert core.pet.functional is PetFunctional.PRIVACY

    fake.set_foreground("Notepad", "notepad.exe")
    await sensor.poll()
    assert core.security.privacy.active_zone is None
    assert core.pet.functional is not PetFunctional.PRIVACY
