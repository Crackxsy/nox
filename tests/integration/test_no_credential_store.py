"""A machine without a credential store - a Linux session with no Secret Service - boots the real
`NoxCore`, reports the store as unavailable, and refuses every change that would relax security:
without the store Nox cannot tell whether a PIN is set, and "cannot tell" is never "no PIN"."""

from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from keyring.backends import fail

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import HealthStatus
from nox.security.gate import PinRequiredError
from tests._ports import free_port_base


@pytest.fixture
async def core_without_keyring(tmp_path: Path):
    keyring.set_keyring(fail.Keyring())  # the suite-wide fixture restores its own afterwards
    base = free_port_base()
    overrides = {
        "paths": {"data_dir": str(tmp_path / "data"), "runtime_dir": str(tmp_path / "runtime")},
        "ipc": {"port": base, "http_port": base + 1},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "security": {"pin_required_for_security_changes": True},
    }
    (tmp_path / "data" / "vault").mkdir(parents=True)
    config = load_config(DEFAULTS_PATH, None, None, overrides)
    core = NoxCore(config, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


async def test_core_boots_and_reports_the_missing_credential_store(
    core_without_keyring: NoxCore,
) -> None:
    report = await core_without_keyring.health.run_once()
    secrets = report.components["secrets"]
    assert secrets.status is HealthStatus.UNAVAILABLE
    assert "Secret Service" in secrets.reason


async def test_relaxing_security_is_refused_without_the_store(
    core_without_keyring: NoxCore,
) -> None:
    gate = core_without_keyring.security.gate
    assert gate.describe() == "unknown"
    with pytest.raises(PinRequiredError, match="cannot be read"):
        await gate.require("any-pin", action="privacy.set", by="dashboard")
