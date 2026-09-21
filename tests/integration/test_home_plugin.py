"""The smart-home feature end to end: a real `NoxCore` spawns the real `nox_plugin_home` worker
against a fake Home Assistant, and a command travels dashboard -> core -> worker -> HA -> back.

Two things only an end-to-end test can show. The `home.*` IPC requests the dashboard calls really
are registered by the extension and really reach the worker's tools through the permission
pipeline; and the hard boundary survives the whole chain - the fake house contains a door lock, an
alarm panel, a valve and a garage door, and none of them is reachable from the outermost layer
either.

**This has never run against a real Home Assistant.** Everything below talks to
`tests/unit/plugins/home/fake_ha_server.py`, which implements the documented protocol. The one
test marked `network` at the end is the one that would use a real instance, and it skips itself
until someone stores a real token.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.home.ipc import HomeCommandInput, HomeLightInput
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.manager import PluginState
from nox.security.profiles import load_profile
from nox.security.secrets import InMemorySecretStore
from tests._ports import free_port_base
from tests.unit.plugins.home.fake_ha_server import VALID_TOKEN, FakeHomeAssistant

pytestmark = pytest.mark.timeout(180)

#: Everything the dashboard's Zuhause page and its Settings tile call.
UI_REQUESTS = (
    "home.status",
    "home.list",
    "home.light",
    "home.switch",
    "home.scene",
    "home.test",
    "home.command",
)


def _home_plugin_dir(tmp_path: Path, *, port: int) -> Path:
    """Copy `plugins/home`, pinning it to the fake server and widening its declared egress.

    The worker is a separate process started with a minimal environment, so it cannot see this
    test's `NOX_USER_CONFIG`; the manifest's `host`/`port` pins (empty in the shipped file) are the
    supported way to point one worker at one fixed instance. The shipped manifest declares the
    production endpoints and the core still requires an explicit declaration, so the test manifest
    gets its own.
    """
    directory = tmp_path / "plugins"
    directory.mkdir()
    dest = directory / "home"
    shutil.copytree(REPO_ROOT / "plugins" / "home", dest)
    manifest_path = dest / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["network"]["egress"] = [f"127.0.0.1:{port}"]
    data["config"]["host"] = "127.0.0.1"
    data["config"]["port"] = port
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _profiles_dir_with_test_port(tmp_path: Path, *, port: int) -> Path:
    """Copy `config/profiles/` and widen `companion`'s loopback allow-list to the fake port.

    The core authorizes a plugin's declared `network.egress` against the *active profile* before
    the worker is spawned; `config/profiles/companion.yaml` itself is never touched, only this
    tmp_path copy.
    """
    directory = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, directory)
    path = directory / "companion.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["permissions"].setdefault("loopback_allowlist", []).append(f"127.0.0.1:{port}")
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


def _config(tmp_path: Path, *, plugins_dir: Path, port: int) -> Any:
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
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["home"], "dir": str(plugins_dir)},
        "home": {"host": "127.0.0.1", "port": port, "min_backoff_s": 0.05, "max_backoff_s": 0.2},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _wait_state(core: NoxCore, state: PluginState, timeout: float = 60) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if core.plugins.state_of("home") is state:
            return
        await asyncio.sleep(0.1)
    record = core.plugins.records().get("home")
    raise AssertionError(
        f"home is {record.state if record else None!r} ({record.reason if record else ''})"
    )


async def _wait_connected(core: NoxCore, timeout: float = 30) -> None:
    """The worker's Home Assistant session is a background task; wait for it to report itself."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            result = await core.plugins.tools.get("home.status.read").handler({})
        except Exception:  # noqa: BLE001 - worker not ready yet
            result = {}
        if result.get("connected"):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("the home plugin never reported a connected status")


@pytest.fixture
async def ha_server() -> Any:
    server = FakeHomeAssistant()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def core(
    tmp_path: Path, ha_server: FakeHomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> Any:
    # The core resolves `home.*` through the shared configuration layers, so the User layer has to
    # be the isolated one rather than the developer's own `%APPDATA%`.
    user_config = tmp_path / "user.yaml"
    user_config.write_text(
        yaml.safe_dump(
            {
                "home": {
                    "host": "127.0.0.1",
                    "port": ha_server.port,
                    "min_backoff_s": 0.05,
                    "max_backoff_s": 0.2,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))

    plugins_dir = _home_plugin_dir(tmp_path, port=ha_server.port)
    profiles_dir = _profiles_dir_with_test_port(tmp_path, port=ha_server.port)
    cfg = _config(tmp_path, plugins_dir=plugins_dir, port=ha_server.port)
    instance = NoxCore(cfg, voice=False, profiles_dir=profiles_dir)
    await asyncio.wait_for(instance.start(), timeout=60)
    # The worker asks the core for `nox/home/access_token`; an in-memory store keeps this test away
    # from the real Windows Credential Manager.
    store = InMemorySecretStore()
    store.set("nox/home/access_token", VALID_TOKEN)
    instance.security.secrets = store  # type: ignore[union-attr]
    instance.plugins._secrets = store  # noqa: SLF001 - the manager caches its own reference
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


def _ctx(role: str = "dashboard") -> RequestContext:
    return RequestContext(
        client_id=f"{role}:1",
        role=role,
        request=Envelope(kind=Kind.REQUEST, name="home.list", src=Source(role=role, id="1")),
    )


async def test_the_home_plugin_runs_and_registers_its_tools(core: NoxCore) -> None:
    await _wait_state(core, PluginState.RUNNING)
    record = core.plugins.records()["home"]
    assert record.process is not None and record.process.poll() is None
    for name in ("home.status.read", "home.list", "home.light", "home.script"):
        assert core.plugins.tools.get(name) is not None
    assert core.plugins.tools.get("home.lock") is None
    assert core.plugins.tools.get("home.unlock") is None
    # The injected registry is the core's own: the executor and the plugin manager have to agree,
    # or every tool call arrives as `tool.unknown`.
    assert core.tool_registry is core.plugins.tools


async def test_the_dashboards_requests_are_registered_by_the_extension(core: NoxCore) -> None:
    for name in UI_REQUESTS:
        assert core.registry.get(name) is not None
        assert core.registry.is_allowed(name, "dashboard") is True
        assert core.registry.is_allowed(name, "plugin") is False


async def test_a_listing_travels_core_to_worker_to_home_assistant_and_back(core: NoxCore) -> None:
    await _wait_state(core, PluginState.RUNNING)
    await _wait_connected(core)
    registration = core.registry.get("home.list")
    assert registration is not None
    payload = await registration.handler(_ctx(), registration.payload_model(area="", domain=""))
    ids = {row["entity_id"] for row in payload["entities"]}
    assert "light.wz_decke" in ids
    assert payload["areas_available"] is True


async def test_the_boundary_holds_through_the_whole_chain(core: NoxCore) -> None:
    """A door lock exists in the fake house; nothing reachable from the dashboard can see it."""
    await _wait_state(core, PluginState.RUNNING)
    await _wait_connected(core)
    listing = core.registry.get("home.list")
    assert listing is not None
    payload = await listing.handler(_ctx(), listing.payload_model(area="", domain=""))
    ids = {row["entity_id"] for row in payload["entities"]}
    for hidden in ("lock.haustuer", "alarm_control_panel.haus", "valve.wasser", "cover.garage"):
        assert hidden not in ids

    switching = core.registry.get("home.switch")
    assert switching is not None
    result = await switching.handler(
        _ctx(), switching.payload_model(entity_ids=["lock.haustuer"], on=False)
    )
    assert result["ok"] is False
    assert result["refused"] is True


async def test_a_german_sentence_is_executed_without_an_ai_round_trip(
    core: NoxCore, ha_server: FakeHomeAssistant
) -> None:
    await _wait_state(core, PluginState.RUNNING)
    await _wait_connected(core)
    registration = core.registry.get("home.command")
    assert registration is not None
    result = await registration.handler(
        _ctx(), HomeCommandInput(text="mach das licht im wohnzimmer aus")
    )
    assert result["matched"] is True
    assert result["tool"] == "home.light"
    call = ha_server.service_calls[-1]
    assert call["domain"] == "light"
    assert call["service"] == "turn_off"


async def test_a_light_toggle_from_the_dashboard_reaches_home_assistant(
    core: NoxCore, ha_server: FakeHomeAssistant
) -> None:
    await _wait_state(core, PluginState.RUNNING)
    await _wait_connected(core)
    registration = core.registry.get("home.light")
    assert registration is not None
    result = await registration.handler(
        _ctx(), HomeLightInput(entity_ids=["light.wz_decke"], on=True)
    )
    assert result["ok"] is True
    assert ha_server.service_calls[-1]["service"] == "turn_on"


@pytest.mark.network
async def test_real_home_assistant_connection() -> None:
    """Skipped until the maintainer points Nox at a real instance and stores a real token.

    No fake data and no fabricated pass: with no token this test skips, and the rest of the suite
    says openly that it has only ever run against the fake server. The probe goes through a real
    `EgressGuard` built from the real `companion` profile, so a host that profile does not allow
    fails here the same way it would in the product.
    """
    from nox.home.probe import probe_home_assistant
    from nox.security.egress import EgressGuard
    from nox.security.secrets import KeyringSecretStore

    token = KeyringSecretStore().get("nox/home/access_token")
    if not token:
        pytest.skip("nox/home/access_token is not set; run `nox secrets set nox/home/access_token`")

    config = load_config(DEFAULTS_PATH)
    profile = load_profile(PROFILES_DIR / "companion.yaml")
    guard = EgressGuard(
        profile=lambda: profile,
        privacy=SimpleNamespace(mode=config.privacy.mode),
        global_allowlist=tuple(config.security.egress_allowlist),
        loopback_allowlist=tuple(config.security.loopback_allowlist),
    )
    result = await probe_home_assistant(config.home, token, guard.client)
    assert result.ok, f"probe failed: {result.code} {result.detail}"
