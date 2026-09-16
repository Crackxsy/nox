"""Plugin runtime end to end: a real `NoxCore` (headless) spawns the real `echo` plugin worker.

Covers ST-11-01's integration criteria: one misconfigured plugin next to a good one, the full
lifecycle over the real IPC hub, a tool call forwarded core -> worker -> back, an emitted event
reaching the bus, the hub refusing an event outside a plugin's declared namespaces, honest health
and the kill switch stopping every plugin worker.
"""

from __future__ import annotations

import asyncio
import random
import shutil
from pathlib import Path

import pytest
import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.core.events import Event, HealthStatus
from nox.ipc.client import IpcClient
from nox.plugins.manager import PluginState

pytestmark = pytest.mark.timeout(180)

BROKEN_MANIFEST = {
    "id": "broken",
    "name": "Broken",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_broken:create",
    # tool outside its own namespace -> must fail validation and never spawn
    "permissions": [{"tool": "obs.scene.switch", "risk": "medium"}],
    "events": {"emits": [], "listens": []},
}


def _plugins_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    shutil.copytree(REPO_ROOT / "plugins" / "echo", directory / "echo")
    (directory / "broken").mkdir()
    (directory / "broken" / "manifest.yaml").write_text(
        yaml.safe_dump(BROKEN_MANIFEST, sort_keys=False), encoding="utf-8"
    )
    return directory


def _config(tmp_path: Path):
    base = random.randint(20000, 60000)  # noqa: S311
    (tmp_path / "vault").mkdir()
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
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["echo", "broken"], "dir": str(_plugins_dir(tmp_path))},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def _wait_state(
    core: NoxCore, plugin_id: str, state: PluginState, timeout: float = 60
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if core.plugins.state_of(plugin_id) is state:
            return
        await asyncio.sleep(0.1)
    record = core.plugins.records().get(plugin_id)
    raise AssertionError(
        f"{plugin_id} is {record.state if record else None!r} ({record.reason if record else ''})"
    )


async def test_echo_plugin_runs_while_a_broken_one_fails(core: NoxCore):
    await _wait_state(core, "echo", PluginState.RUNNING)
    records = core.plugins.records()
    assert records["broken"].state is PluginState.FAILED
    assert "namespace" in records["broken"].reason
    assert records["echo"].process is not None and records["echo"].process.poll() is None
    assert records["echo"].client_id == "plugin:echo"


async def test_tool_is_registered_and_forwarded_to_the_worker(core: NoxCore):
    await _wait_state(core, "echo", PluginState.RUNNING)
    spec = core.plugins.tools.get("echo.ping")
    assert spec is not None and spec.risk == "read" and spec.local is True
    pong: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

    def collect(event: Event) -> None:
        if not pong.done():
            pong.set_result(event)

    unsubscribe = core.bus.subscribe("echo.pong", collect)
    try:
        result = await asyncio.wait_for(spec.handler({"text": "hallo"}), timeout=30)
        assert result == {"text": "hallo", "greeting": "pong"}
        event = await asyncio.wait_for(pong, timeout=10)
        assert event.payload == {"text": "hallo"}
    finally:
        unsubscribe()


async def test_hub_rejects_events_outside_a_plugins_namespaces(core: NoxCore):
    """A plugin connection without declared services may publish nothing at all."""
    await _wait_state(core, "echo", PluginState.RUNNING)
    seen: list[Event] = []
    unsubscribe = core.bus.subscribe("echo.*", seen.append)
    token = core.tokens.issue_worker_token("plugin:rogue")
    rogue = IpcClient(core.hub.url, token, "plugin", "plugin:rogue", client_version="0.1.0")
    await rogue.connect()
    try:
        await rogue.send_event("echo.pong", {"text": "spoofed"})
        await asyncio.sleep(0.5)
        assert [e.payload for e in seen] == []
    finally:
        await rogue.close()
        unsubscribe()


async def test_plugin_health_is_reported_honestly(core: NoxCore):
    await _wait_state(core, "echo", PluginState.RUNNING)
    await core.health.run_once()
    current = core.health.current()
    assert current["plugin.echo"].status is HealthStatus.AVAILABLE
    assert current["plugin.broken"].status is HealthStatus.UNAVAILABLE
    assert "namespace" in current["plugin.broken"].reason


async def test_kill_switch_stops_the_plugin_worker(core: NoxCore):
    await _wait_state(core, "echo", PluginState.RUNNING)
    process = core.plugins.records()["echo"].process
    assert process is not None
    report = await core.security.killswitch.engage("ui", "test")
    assert report.hooks.get("plugins.stop") == "ok"
    assert core.plugins.state_of("echo") is PluginState.STOPPED
    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=10)
    assert process.poll() is not None
    assert core.plugins.tools.get("echo.ping") is None
