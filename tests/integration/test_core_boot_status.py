"""The heartbeat's `booting` flag: true until `NoxCore.start()` has run to its end.

The supervisor reads it to tell a core that is still installing - and may legitimately stall on a
cold machine - from one that hangs (`nox.supervisor.main`)."""

from __future__ import annotations

from pathlib import Path

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from tests._ports import free_port_base


def _config(tmp_path: Path):
    base = free_port_base()
    overrides = {
        "paths": {
            name: str(tmp_path / name)
            for name in (
                "data_dir",
                "vault_dir",
                "index_dir",
                "database_dir",
                "cache_dir",
                "backups_dir",
                "runtime_dir",
                "logs_dir",
            )
        },
        "ipc": {"port": base, "http_port": base + 1},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault_dir").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def test_the_core_reports_booting_until_it_has_started(tmp_path: Path) -> None:
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    assert core._supervisor_status()["booting"] is True  # noqa: SLF001 - the heartbeat's payload
    await core.start()
    try:
        assert core._supervisor_status()["booting"] is False  # noqa: SLF001
    finally:
        await core.stop()
