"""OBS goes live -> Nox answers Twitch chat, without anyone saying "lass uns streamen".

A real `NoxCore` (headless, no voice) runs the real `obs` and `twitch` plugin workers against a
fake obs-websocket server and a fake Twitch IRC server. The core starts in the everyday companion
mode, where only the `obs` plugin runs. When the fake OBS reports the stream going live, the
stream extension switches to stream mode by itself, the `twitch` plugin starts and joins, and a
chat message addressed to Nox gets an answer through the real responder and `twitch.chat.send`.
When OBS stops, Nox returns to companion mode.

Nothing here reaches OBS or Twitch: both are local fakes on ephemeral ports, the credentials live
in an in-process fake keyring, the Twitch live check is off, and the AI answer comes from the
local rules provider.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.boot.extensions import install_extensions
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.plugins.manager import PluginState
from nox.security import service as security_service
from nox.security.model import Decision
from nox.security.secrets import KeyringSecretStore
from nox.stream.overlay import OverlaySetupRequest
from tests._ports import free_port_base
from tests.unit.plugins.obs.fake_obs_server import FakeObsServer
from tests.unit.plugins.twitch.fake_irc_server import FakeIrcServer

pytestmark = pytest.mark.timeout(240)

_KEYRING = {"nox/twitch/oauth_token": "oauth:s3cret", "nox/twitch/bot_username": "noxbot"}


class _FakeKeyringBackend:
    def get_password(self, _service: str, username: str) -> str | None:
        return _KEYRING.get(username)

    def set_password(self, _service: str, username: str, value: str) -> None:
        _KEYRING[username] = value

    def delete_password(self, _service: str, username: str) -> None:
        _KEYRING.pop(username, None)


def _plugins_dir(tmp_path: Path, *, obs_port: int, irc_port: int) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    for plugin in ("obs", "twitch"):
        shutil.copytree(REPO_ROOT / "plugins" / plugin, directory / plugin)
    obs = directory / "obs" / "manifest.yaml"
    data = yaml.safe_load(obs.read_text(encoding="utf-8"))
    data["config"].update(port=obs_port, min_backoff_s=0.05, max_backoff_s=0.2)
    data["network"]["egress"] = [f"127.0.0.1:{obs_port}"]
    obs.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    twitch = directory / "twitch" / "manifest.yaml"
    data = yaml.safe_load(twitch.read_text(encoding="utf-8"))
    data["config"].update(
        host="127.0.0.1",
        port=irc_port,
        tls=False,
        channel="testchannel",
        min_backoff_s=0.05,
        max_backoff_s=0.2,
        rate_limit_min_gap_s=0.0,
    )
    data["network"]["egress"] = [f"127.0.0.1:{irc_port}"]
    twitch.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _profiles_dir(tmp_path: Path, *ports: int) -> Path:
    directory = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, directory)
    for name in ("companion.yaml", "stream.yaml"):
        path = directory / name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["permissions"]["loopback_allowlist"].extend(f"127.0.0.1:{p}" for p in ports)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _config(tmp_path: Path, plugins_dir: Path) -> Any:
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            name: str(tmp_path / name.removesuffix("_dir"))
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
        "privacy": {"mode": "balanced"},
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["obs", "twitch"], "dir": str(plugins_dir)},
        "ai": {"router": {"default_reasoner": "rules", "fallback_chain": ["rules"]}},
        "stream": {
            "relevance": {"mention_cooldown_s": 0.0, "channel_cooldown_s": 0.0},
            "twitch": {"channel": "testchannel"},
            "auto_session": {"twitch_live_poll_s": 0.0},
        },
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _until(check: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
async def servers() -> Any:
    obs = FakeObsServer(password=None)
    irc = FakeIrcServer()
    await obs.start()
    await irc.start()
    try:
        yield obs, irc
    finally:
        await obs.stop()
        await irc.stop()


@pytest.fixture
async def core(tmp_path: Path, servers: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    obs, irc = servers
    monkeypatch.setattr(
        security_service,
        "open_secret_store",
        lambda *a, **kw: KeyringSecretStore(backend=_FakeKeyringBackend()),
    )
    plugins = _plugins_dir(tmp_path, obs_port=obs.port, irc_port=irc.port)
    profiles = _profiles_dir(tmp_path, obs.port, irc.port)
    core = NoxCore(_config(tmp_path, plugins), voice=False, profiles_dir=profiles, extensions=False)
    await asyncio.wait_for(core.start(), timeout=90)
    # Only what this flow needs: no settings extension, so no Twitch token check reaches out.
    core.extensions = await install_extensions(core, ["proactive", "stream"])
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=60)


def _mode(core: NoxCore) -> str:
    return str(core.state.get("assistant.mode"))


async def test_obs_going_live_starts_the_session_and_nox_answers_chat(
    core: NoxCore, servers: Any
) -> None:
    obs, irc = servers
    assert _mode(core) == "companion"
    await _until(lambda: core.plugins.state_of("obs") is PluginState.RUNNING, "obs plugin")
    assert core.plugins.state_of("twitch") is not PluginState.RUNNING  # stream profile only
    await _until(lambda: obs.identify_count >= 1, "obs plugin connected")

    await obs.broadcast_event("StreamStateChanged", {"outputActive": True})
    await _until(lambda: _mode(core) == "stream", "stream mode")
    assert core.stream_sessions.status()["active"] is True
    await _until(lambda: core.plugins.state_of("twitch") is PluginState.RUNNING, "twitch plugin")
    await _until(lambda: irc.joined_channel == "#testchannel", "twitch joined")

    await irc.send_privmsg(text="hey nox, how is it going?", user_id="7", login="viewer7")
    await _until(
        lambda: any(line.startswith("PRIVMSG #testchannel") for line in irc.received_lines),
        "an answer in chat",
    )

    await obs.broadcast_event("StreamStateChanged", {"outputActive": False})
    await _until(lambda: _mode(core) == "companion", "back to companion mode")


async def test_overlay_role_can_reach_nothing_but_state_get(core: NoxCore) -> None:
    registry = core.registry
    assert registry is not None
    reachable = [n for n in registry.names() if registry.is_allowed(n, "overlay")]
    assert reachable == ["state.get"]


async def _answer_permissions(core: NoxCore, allow: bool, asked: list[dict[str, Any]]) -> None:
    """Plays the user at the permission dialog for every question the core asks."""

    def on_request(event: Event) -> None:
        asked.append(dict(event.payload))
        core.security.engine.reply(
            event.payload["request_id"], Decision.ALLOW if allow else Decision.DENY
        )

    core.bus.subscribe(E.SECURITY_PERMISSION_REQUESTED, on_request)


async def test_overlay_setup_end_to_end_gives_obs_a_working_overlay_url(
    core: NoxCore, servers: Any
) -> None:
    obs, _irc = servers
    await _until(lambda: obs.identify_count >= 1, "obs plugin connected")
    obs.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs.set_response("GetInputList", {"inputs": []})
    asked: list[dict[str, Any]] = []
    await _answer_permissions(core, True, asked)
    runtime = core.extensions["stream"]
    result = await runtime.overlay.setup(OverlaySetupRequest(), agent="dashboard", origin="local")
    assert result["ok"] is True, result
    assert [a["action"] for a in asked] == ["overlay.setup"]
    created = dict(obs.requests)["CreateInput"]
    url = created["inputSettings"]["url"]
    assert f":{core.http.port}/pet/?overlay=1#overlay=" in url
    token = url.split("#overlay=", 1)[1]
    assert core.tokens.authenticate(token, "overlay", "overlay:test").ok
    assert not core.tokens.authenticate(token, "dashboard", "dashboard:test").ok


async def test_generic_obs_requests_through_the_real_core(core: NoxCore, servers: Any) -> None:
    obs, _irc = servers
    await _until(lambda: obs.identify_count >= 1, "obs plugin connected")
    asked: list[dict[str, Any]] = []
    await _answer_permissions(core, True, asked)
    executor = core.tool_executor

    read = await executor.call(
        "nox.chat", "obs.request", {"request_type": "GetSceneList"}, mode="companion"
    )
    assert read.ok and asked == []  # reads are not asked

    created = await executor.call(
        "nox.chat",
        "obs.request",
        {"request_type": "CreateScene", "request_data": {"sceneName": "Just Chatting"}},
        mode="companion",
    )
    assert created.ok and [a["action"] for a in asked] == ["request.CreateScene"]

    stop = await executor.call(
        "nox.chat", "obs.request", {"request_type": "StopStream"}, mode="companion"
    )
    assert not stop.ok and len(asked) == 1  # refused without a question
    assert "StopStream" not in [t for t, _ in obs.requests]
