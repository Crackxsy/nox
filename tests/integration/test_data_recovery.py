"""Fault injection at boot: a damaged database header, and a user.yaml with one invalid key.

Nox must start, keep what it can, and say loudly what happened - in health and in the audit log.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import NoxConfig, load_config
from nox.core.events import HealthStatus
from tests._ports import free_port_base


def _config(tmp_path: Path, user: Path | None = None) -> NoxConfig:
    base = free_port_base()
    (tmp_path / "vault").mkdir(exist_ok=True)
    return load_config(
        DEFAULTS_PATH,
        user,
        None,
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "vault_dir": str(tmp_path / "vault"),
                "runtime_dir": str(tmp_path / "runtime"),
            },
            "ipc": {"port": base, "http_port": base + 1},
            "health": {"check_interval_s": 3600},
            "logging": {"level": "WARNING"},
        },
    )


async def test_a_damaged_database_header_is_set_aside_and_nox_starts_loudly(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    database = Path(config.paths.database_dir) / "nox.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"this is not a database at all" * 100)
    core = NoxCore(config, voice=False, profiles_dir=PROFILES_DIR, extensions=False)

    await asyncio.wait_for(core.start(), 60)
    try:
        assert core.health is not None and core.security is not None
        db = core.health.current()["db"]
        assert db.status is HealthStatus.LIMITED
        assert "set aside" in db.reason and "nox.db.corrupt-" in db.reason
        moved = list(database.parent.glob("nox.db.corrupt-*"))
        assert len(moved) == 1 and moved[0].read_bytes().startswith(b"this is not a database")
        actions = [entry.action for entry in core.security.audit_store.entries(limit=50)]
        assert "db.recovered" in actions
        assert core.security.audit_store.verify_chain()  # the new chain starts clean
    finally:
        await asyncio.wait_for(core.stop(), 30)


async def test_one_invalid_key_in_user_yaml_keeps_the_rest_of_the_users_choices(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user.yaml"
    user.write_text(
        yaml.safe_dump({"privacy": {"mode": "private"}, "voice": {"barge_in": "sometimes"}}),
        encoding="utf-8",
    )
    config = _config(tmp_path, user)
    core = NoxCore(config, voice=False, profiles_dir=PROFILES_DIR, extensions=False)

    await asyncio.wait_for(core.start(), 60)
    try:
        assert core.security is not None and core.health is not None
        assert core.security.privacy.mode.value == "private"  # not the default "balanced"
        assert [w.key for w in config.warnings] == ["voice.barge_in"]
        # A cloud provider is not even probed while privacy forbids cloud models.
        claude = core.health.current()["ai.claude_code"]
        assert claude.status is HealthStatus.UNAVAILABLE and "not probed" in claude.reason
        # The retention job is scheduled and says so.
        assert "retention" in core.health.current()
    finally:
        await asyncio.wait_for(core.stop(), 30)
