"""#28: `nox.proactive.install.install` wired against a real headless `NoxCore`, proving the
notification store now survives a restart - a notification created (and a second one dismissed)
before `core.stop()` is still listed (with its dismissal) after a fresh `NoxCore` boots against the
same database, exactly like `tests/integration/test_memory.py`'s core fixture pattern but with two
sequential core instances instead of one."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.proactive.install import install as install_proactive
from nox.proactive.service import ProactiveService

pytestmark = pytest.mark.timeout(120)


def _config(tmp_path: Path):
    base = random.randint(20000, 60000)  # noqa: S311
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
        "privacy": {"mode": "balanced"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir(exist_ok=True)
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _boot(tmp_path: Path) -> tuple[NoxCore, ProactiveService]:
    """A booted core plus the proactive service the extension returned."""
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await asyncio.wait_for(core.start(), timeout=60)
    return core, install_proactive(core).service


async def test_notification_and_dismissal_survive_a_restart(tmp_path: Path) -> None:
    first, first_proactive = await _boot(tmp_path)
    try:
        # "urgent"/"security" bypasses zone/privacy-mode/quiet-hours gating (B.13) - keeps this
        # test deterministic regardless of the wall-clock time it happens to run at.
        kept = await first_proactive.notify("urgent", "security", "You have 3 new emails")
        assert kept.allowed is True
        dismissed_decision = await first_proactive.notify("urgent", "resources", "GPU at 95C")
        assert dismissed_decision.allowed is True

        status_before = first_proactive.status()
        assert [r.text for r in status_before.recent] == ["GPU at 95C", "You have 3 new emails"]
        dismissed_id = status_before.recent[0].id
        assert await first_proactive.dismiss(dismissed_id) is True
    finally:
        await asyncio.wait_for(first.stop(), timeout=30)

    second, second_proactive = await _boot(tmp_path)
    try:
        status_after = second_proactive.status()
        by_text = {r.text: r for r in status_after.recent}
        assert set(by_text) == {"You have 3 new emails", "GPU at 95C"}
        assert by_text["You have 3 new emails"].dismissed_at is None
        assert by_text["GPU at 95C"].dismissed_at is not None
        assert by_text["GPU at 95C"].id == dismissed_id
    finally:
        await asyncio.wait_for(second.stop(), timeout=30)


async def test_notification_store_is_db_backed_when_installed(tmp_path: Path) -> None:
    core, proactive = await _boot(tmp_path)
    try:
        # `install()` wires the core database into the store rather than leaving it in memory.
        assert proactive.store._repo is not None  # noqa: SLF001 - the point of this test
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)
