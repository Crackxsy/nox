"""Security state across a restart of the real core.

Each test boots a core, changes something, shuts it down and boots a second core on the same data
folder - which is what a crash-respawn or a watchdog restart does. Nothing may come back less
private or less stopped than it went down; a database that was deleted in between is noticed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.state import PrivacyMode
from nox.ipc.client import IpcClient
from nox.ipc.errors import IpcError
from nox.ipc.role_tokens import derive_role_token
from nox.ipc.tokens import read_session_token
from tests._ports import free_port_base


def _config_factory(tmp_path: Path) -> Callable[[], Any]:
    """A fresh configuration per boot: the same folders, new ports."""
    (tmp_path / "vault").mkdir(exist_ok=True)

    def make() -> Any:
        base = free_port_base()
        overrides = {
            "paths": {
                name: str(tmp_path / folder)
                for name, folder in (
                    ("data_dir", "data"),
                    ("vault_dir", "vault"),
                    ("index_dir", "index"),
                    ("database_dir", "db"),
                    ("cache_dir", "cache"),
                    ("backups_dir", "backups"),
                    ("runtime_dir", "runtime"),
                    ("logs_dir", "logs"),
                )
            },
            "ipc": {"port": base, "http_port": base + 1},
            "privacy": {"mode": "balanced"},
            "health": {"check_interval_s": 3600},
            "logging": {"level": "WARNING"},
        }
        return load_config(DEFAULTS_PATH, None, None, overrides)

    return make


@asynccontextmanager
async def running(config: Any) -> AsyncIterator[NoxCore]:
    core = NoxCore(config, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def _client(core: NoxCore) -> IpcClient:
    assert core.hub is not None
    session = read_session_token(Path(core.config.paths.runtime_dir))
    # The dashboard's own role token; the shell's session token is refused for any other role.
    token = derive_role_token(session, "dashboard")
    client = IpcClient(core.hub.url, token, "dashboard", "test-dashboard", client_version="0.1.0")
    await client.connect()
    return client


async def test_privacy_mode_and_kill_switch_survive_a_restart(tmp_path: Path) -> None:
    make = _config_factory(tmp_path)
    async with running(make()) as first:
        client = await _client(first)
        try:
            await client.request("privacy.set", {"mode": "offline"})
            await client.request("security.kill", {"reason": "user stop", "origin": "ui"})
        finally:
            await client.close()

    async with running(make()) as second:
        assert second.security is not None and second.state is not None
        assert second.security.privacy.mode is PrivacyMode.OFFLINE
        assert second.security.killswitch.is_engaged()
        assert second.state.get("system.level") == "safe_mode"
        assert second.state.get("privacy.mode") == "offline"
        client = await _client(second)
        try:
            with pytest.raises(IpcError):
                await client.request("chat.send", {"text": "hallo", "speak": False}, timeout=10)
            assert (await client.request("security.resume", {}))["ok"] is True
            assert second.state.get("system.level") == "running"
        finally:
            await client.close()

    async with running(make()) as third:
        assert third.security is not None
        assert not third.security.killswitch.is_engaged()
        assert third.security.privacy.mode is PrivacyMode.OFFLINE  # the mode stays as set


async def test_a_deleted_database_boots_in_safe_mode_until_acknowledged(tmp_path: Path) -> None:
    make = _config_factory(tmp_path)
    async with running(make()) as first:
        assert first.state is not None and first.state.get("system.level") == "running"

    for leftover in (tmp_path / "db").glob("nox.db*"):
        leftover.unlink()

    async with running(make()) as second:
        assert second.security is not None and second.state is not None
        assert second.security.killswitch.is_engaged()
        assert second.security.killswitch.origin == "audit"
        assert second.state.get("system.level") == "safe_mode"
        client = await _client(second)
        try:
            with pytest.raises(IpcError):
                await client.request("chat.send", {"text": "hallo", "speak": False}, timeout=10)
            assert (await client.request("security.resume", {}))["ok"] is True
        finally:
            await client.close()
        second.security.audit.flush()
        actions = [e.action for e in second.security.audit_store.entries(limit=1000)]
        assert "audit.verify" in actions and "audit.break_acknowledged" in actions

    async with running(make()) as third:
        assert third.security is not None and third.state is not None
        assert not third.security.killswitch.is_engaged()
        assert third.state.get("system.level") == "running"
