"""Twitch plugin, end to end: a real `NoxCore` (headless) spawns the real `nox_plugin_twitch`
worker against a fake IRC server, and a chat message / tool call is forwarded core -> worker ->
IRC -> back. Modelled on `tests/integration/test_obs_plugin.py`.

No Twitch bot account exists yet (ES-01), so there is no "real Twitch" network test here (unlike
`test_obs_plugin.py`'s `test_real_obs_connection_and_scene_list`) - nothing to skip against.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

import nox.security.service as security_service
from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.core.events import Event
from nox.ipc.errors import IpcError
from nox.plugins.manager import PluginState
from nox.security.secrets import KeyringSecretStore
from tests._ports import free_port_base
from tests.unit.plugins.twitch.fake_irc_server import FakeIrcServer

pytestmark = pytest.mark.timeout(180)

# No Twitch bot account exists yet (ES-01): `nox/twitch/oauth_token`/`bot_username` are never set
# in the real Windows Credential Manager this machine uses. `SecurityContext.build` falls back to a
# real `KeyringSecretStore()` when `NoxCore` does not hand it one (and `NoxCore.__init__` takes no
# such override - out of this plugin's scope to add), so this fake, in-process-only keyring backend
# is swapped in for the credential lookup only (ENGINEERING.md: "tests use an in-memory backend").
# It never touches the real Credential Manager and reverts automatically via `monkeypatch`.
_FAKE_KEYRING_VALUES = {
    "nox/twitch/oauth_token": "s3cret",
    "nox/twitch/bot_username": "noxbot",
}


class _FakeKeyringBackend:
    def get_password(self, _service: str, username: str) -> str | None:
        return _FAKE_KEYRING_VALUES.get(username)

    def set_password(self, _service: str, username: str, value: str) -> None:
        _FAKE_KEYRING_VALUES[username] = value

    def delete_password(self, _service: str, username: str) -> None:
        _FAKE_KEYRING_VALUES.pop(username, None)


def _twitch_plugin_dir(tmp_path: Path, *, port: int) -> Path:
    """Copy `plugins/twitch`, pointing its config at the fake IRC server (plain TCP, no TLS) and
    widening the declared egress to that ephemeral loopback port (the shipped manifest declares
    the fixed production host/port; ADR-013 still requires an explicit declaration, so the test
    manifest gets its own - same pattern as `test_obs_plugin.py`'s `_obs_plugin_dir`)."""
    directory = tmp_path / "plugins"
    directory.mkdir()
    dest = directory / "twitch"
    shutil.copytree(REPO_ROOT / "plugins" / "twitch", dest)
    manifest_path = dest / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["config"]["host"] = "127.0.0.1"
    data["config"]["port"] = port
    data["config"]["tls"] = False
    data["config"]["channel"] = "testchannel"
    data["config"]["min_backoff_s"] = 0.05
    data["config"]["max_backoff_s"] = 0.2
    data["config"]["rate_limit_max_messages"] = 20
    data["config"]["rate_limit_window_s"] = 30.0
    data["config"]["rate_limit_min_gap_s"] = 0.0  # isolate the count cap from the gap constraint
    data["network"]["egress"] = [f"127.0.0.1:{port}"]
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _config(tmp_path: Path, *, plugins_dir: Path):
    base = free_port_base()
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
        "plugins": {"enabled": ["twitch"], "dir": str(plugins_dir)},
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


async def _wait_joined(core: NoxCore, timeout: float = 30) -> None:
    """The worker's IRC connection is a background task started from `plugin.start()`; give it a
    moment after the plugin is RUNNING before calling a tool that needs it joined."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            result = await core.plugins.tools.get("twitch.chat.status.read").handler({})
        except Exception:  # noqa: BLE001 - worker not ready yet
            result = {}
        if result.get("joined"):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("twitch plugin never joined the fake IRC channel")


@pytest.fixture
async def irc_server():
    server = FakeIrcServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _profiles_dir_with_test_port(tmp_path: Path, *, port: int) -> Path:
    """Copy `config/profiles/` and widen the `stream` profile's `loopback_allowlist` to the fake
    server's ephemeral port - the core authorizes a plugin's declared `network.egress` against the
    *active profile* at spawn time (ADR-013), and `config/profiles/stream.yaml` (owned by the
    security/config area) is not touched: only this tmp_path copy is. The real, non-loopback
    Twitch hosts already live in that file's `egress_allowlist` (ST-11-04)."""
    directory = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, directory)
    stream_path = directory / "stream.yaml"
    data = yaml.safe_load(stream_path.read_text(encoding="utf-8"))
    data["permissions"]["loopback_allowlist"].append(f"127.0.0.1:{port}")
    stream_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


@pytest.fixture
async def core(tmp_path: Path, irc_server: FakeIrcServer, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        security_service,
        "KeyringSecretStore",
        lambda *a, **kw: KeyringSecretStore(backend=_FakeKeyringBackend()),
    )
    plugins_dir = _twitch_plugin_dir(tmp_path, port=irc_server.port)
    profiles_dir = _profiles_dir_with_test_port(tmp_path, port=irc_server.port)
    cfg = _config(tmp_path, plugins_dir=plugins_dir)
    core = NoxCore(cfg, voice=False, profiles_dir=profiles_dir)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def test_twitch_plugin_runs_and_registers_its_tools(core: NoxCore) -> None:
    await _wait_state(core, "twitch", PluginState.RUNNING)
    records = core.plugins.records()
    assert records["twitch"].process is not None and records["twitch"].process.poll() is None
    for name in ("twitch.chat.send", "twitch.chat.status.read"):
        assert core.plugins.tools.get(name) is not None


async def test_chat_message_reaches_the_core_bus(core: NoxCore, irc_server: FakeIrcServer) -> None:
    await _wait_state(core, "twitch", PluginState.RUNNING)
    await _wait_joined(core)
    seen: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

    def collect(event: Event) -> None:
        if not seen.done():
            seen.set_result(event)

    unsubscribe = core.bus.subscribe("twitch.chat_message", collect)
    try:
        await irc_server.send_privmsg(text="hello nox", user_id="99", login="viewer99")
        event = await asyncio.wait_for(seen, timeout=15)
        assert event.payload["text"] == "hello nox"
        assert event.payload["viewer_id"] == "99"
        assert event.payload["channel"] == "public"
    finally:
        unsubscribe()


async def test_chat_send_is_rate_limited_on_the_21st_message(core: NoxCore) -> None:
    await _wait_state(core, "twitch", PluginState.RUNNING)
    await _wait_joined(core)
    spec = core.plugins.tools.get("twitch.chat.send")
    assert spec is not None and spec.risk == "low"
    for i in range(20):
        result = await asyncio.wait_for(spec.handler({"text": f"message {i}"}), timeout=10)
        assert result == {"sent": True, "text": f"message {i}"}
    # `_send` raises `IpcError` (not a plain exception) precisely so its message and code survive
    # the worker's IPC boundary verbatim (see the comment on `TwitchPlugin._send`).
    with pytest.raises(IpcError, match="rate limit"):
        await asyncio.wait_for(spec.handler({"text": "one too many"}), timeout=10)
