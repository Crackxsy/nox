"""OBS plugin, end to end: a real `NoxCore` (headless) spawns the real `nox_plugin_obs` worker
against a fake obs-websocket v5 server, and a tool call is forwarded core -> worker -> OBS -> back.
Modelled on `tests/integration/test_plugins.py`.

The fake server runs without a password (`FakeObsServer(password=None)`) so this test never needs
to reach through `plugin.secret.get` into a real secret store; the auth path itself is covered by
`tests/unit/plugins/obs/test_ws_client.py` against the same fake server with a password set.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.plugins.manager import PluginState
from tests.unit.plugins.obs.fake_obs_server import FakeObsServer, default_scene_list

pytestmark = pytest.mark.timeout(180)

# The `test_real_obs_connection_and_scene_list` network test imports `nox_plugin_obs` directly
# in-process (there is no worker subprocess to do it for us there); every other test here talks to
# the real worker subprocess, which inserts this path itself (`nox.worker.plugin.load_entry`).
_OBS_SRC = REPO_ROOT / "plugins" / "obs" / "src"
if str(_OBS_SRC) not in sys.path:
    sys.path.insert(0, str(_OBS_SRC))


def _obs_plugin_dir(tmp_path: Path, *, port: int) -> Path:
    """Copy `plugins/obs`, pointing its config at the fake server and widening the declared
    egress to that ephemeral loopback port (the shipped manifest declares the fixed production
    port 4455; ADR-013 still requires an explicit declaration, so the test manifest gets its
    own)."""
    directory = tmp_path / "plugins"
    directory.mkdir()
    dest = directory / "obs"
    shutil.copytree(REPO_ROOT / "plugins" / "obs", dest)
    manifest_path = dest / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["config"]["port"] = port
    data["config"]["min_backoff_s"] = 0.05
    data["config"]["max_backoff_s"] = 0.2
    data["network"]["egress"] = [f"127.0.0.1:{port}"]
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _config(tmp_path: Path, *, plugins_dir: Path):
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
        "privacy": {"mode": "balanced"},
        "security": {"profile": "stream"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["obs"], "dir": str(plugins_dir)},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


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


async def _wait_tool_identified(core: NoxCore, timeout: float = 30) -> None:
    """The worker's OBS connection is a background task started from `plugin.start()`; give it a
    moment after the plugin is RUNNING before calling a tool that needs it connected."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            result = await core.plugins.tools.get("obs.status.read").handler({})
        except Exception:  # noqa: BLE001 - worker not ready yet
            result = {}
        if result.get("connected"):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("obs plugin never reported a connected status")


@pytest.fixture
async def obs_server():
    server = FakeObsServer(password=None)
    server.set_response("GetSceneList", lambda _d: default_scene_list(["Start", "Live"], "Start"))
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _profiles_dir_with_test_port(tmp_path: Path, *, port: int) -> Path:
    """Copy `config/profiles/` and widen the `stream` profile's `loopback_allowlist` to the fake
    server's ephemeral port - the core authorizes a plugin's declared `network.egress` against the
    *active profile* at spawn time (ADR-013), and `config/profiles/stream.yaml` (owned by the
    security/config area) is not touched: only this tmp_path copy is."""
    directory = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, directory)
    stream_path = directory / "stream.yaml"
    data = yaml.safe_load(stream_path.read_text(encoding="utf-8"))
    data["permissions"]["loopback_allowlist"].append(f"127.0.0.1:{port}")
    stream_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


@pytest.fixture
async def core(tmp_path: Path, obs_server: FakeObsServer):
    plugins_dir = _obs_plugin_dir(tmp_path, port=obs_server.port)
    profiles_dir = _profiles_dir_with_test_port(tmp_path, port=obs_server.port)
    cfg = _config(tmp_path, plugins_dir=plugins_dir)
    core = NoxCore(cfg, voice=False, profiles_dir=profiles_dir)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def test_obs_plugin_runs_and_registers_its_tools(core: NoxCore) -> None:
    await _wait_state(core, "obs", PluginState.RUNNING)
    records = core.plugins.records()
    assert records["obs"].process is not None and records["obs"].process.poll() is None
    for name in (
        "obs.status.read",
        "obs.scenes.list",
        "obs.scene.switch",
        "obs.privacy_scene.activate",
        "obs.preflight.check",
    ):
        assert core.plugins.tools.get(name) is not None


async def test_obs_scenes_list_is_forwarded_to_the_worker_and_back(
    core: NoxCore, obs_server: FakeObsServer
) -> None:
    await _wait_state(core, "obs", PluginState.RUNNING)
    await _wait_tool_identified(core)
    spec = core.plugins.tools.get("obs.scenes.list")
    assert spec is not None and spec.risk == "read"
    result = await asyncio.wait_for(spec.handler({}), timeout=30)
    assert result == {"current_scene": "Start", "scenes": ["Start", "Live"]}
    assert obs_server.requests[-1][0] == "GetSceneList"


# ---- real OBS (this machine, port 4455, secret required) -----------------------------------------


@pytest.mark.network
async def test_real_obs_connection_and_scene_list() -> None:
    """Facts: OBS 32.2.1 with obs-websocket v5 on 127.0.0.1:4455, auth on. Skipped until the
    maintainer runs `nox secrets set nox/obs/websocket_password` - no fake data, no fabricated
    pass."""
    from nox.security.secrets import KeyringSecretStore

    password = KeyringSecretStore().get("nox/obs/websocket_password")
    if not password:
        pytest.skip("nox/obs/websocket_password is not set; run `nox secrets set ...` first")

    from nox_plugin_obs.ws_client import ObsWebSocketClient

    async def _password() -> str:
        return password

    async def _ignore(_name: str, _data: dict) -> None:
        return None

    client = ObsWebSocketClient(
        "ws://127.0.0.1:4455", password_provider=_password, on_event=_ignore, request_timeout_s=10
    )
    client.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while loop.time() < deadline:
            if client.identified:
                break
            await asyncio.sleep(0.1)
        if not client.identified:
            pytest.fail(f"could not connect to real OBS: {client.last_error}")
        result = await client.request("GetSceneList")
        assert "scenes" in result
    finally:
        await client.stop()
