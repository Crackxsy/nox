"""Saying "lass uns streamen" to a real core switches the mode *and* the security profile.

The profile is what lets Twitch reach its chat and OBS its websocket. A switch that changed only
the mode - which is what a preset did - left both locked out.
"""

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
        # No real Twitch/OBS workers in a test: the switch itself is what is checked here.
        "plugins": {"enabled": []},
    }
    (tmp_path / "vault_dir").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def test_lass_uns_streamen_switches_mode_and_profile(tmp_path: Path) -> None:
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await core.start()
    try:
        assert core.orchestrator is not None and core.security is not None
        turn = await core.orchestrator.handle_text("Lass uns streamen", speak=False)
        assert turn.fast_path == "mode"
        assert core.state.get("assistant.mode") == "stream"
        assert core.security.engine.active_profile().id == "stream"
        assert "Stream-Modus ist an" in turn.response

        turn = await core.orchestrator.handle_text("Stream beenden", speak=False)
        assert core.state.get("assistant.mode") == "companion"
        assert core.security.engine.active_profile().id == "companion"
    finally:
        await core.stop()
