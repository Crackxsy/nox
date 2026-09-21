"""Rocket League plugin, end to end: a real `NoxCore` (headless) spawns the real `nox_plugin_rl`
worker, fake game detection (the plugin's own test process name, always running) drives a mode
transition, and a synthetic replay fixture dropped into the watched folder produces a real
`rl.replay_parsed` event via the pure-Python header parser (ST-12-01/02/03/06 wiring).
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import sys
from pathlib import Path

import pytest
import yaml

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.plugins.manager import PluginState
from tests._ports import free_port_base

pytestmark = pytest.mark.timeout(120)


def _build_fixture_replay() -> bytes:
    """A minimal, real-format `.replay` header (engine 868/licensee 32, so `net_version` is
    present) with a small property tree: TeamSize, MapName, one PlayerStats entry - enough for
    `parse_header` to report `parse_status == "ok"` and a populated summary."""

    def s(text: str) -> bytes:
        raw = text.encode("latin1") + b"\x00"
        return struct.pack("<i", len(raw)) + raw

    def prop(name: str, ptype: str, value: bytes) -> bytes:
        # `size` (the redundant trailing-value-size hint) is never trusted by the parser for a
        # type it understands structurally - Int/Str/Name/Float here - so any value works; 0 is
        # simplest and matches what real IntProperty entries carry.
        out = s(name) + s(ptype)
        out += struct.pack("<i", 0)  # size hint (unused for these types)
        out += struct.pack("<i", 0)  # array_index
        out += value
        return out

    body = b""
    body += prop("TeamSize", "IntProperty", struct.pack("<i", 1))
    body += prop("Team0Score", "IntProperty", struct.pack("<i", 2))
    body += prop("Team1Score", "IntProperty", struct.pack("<i", 1))
    body += prop("PrimaryPlayerTeam", "IntProperty", struct.pack("<i", 0))
    body += prop("WinningTeam", "IntProperty", struct.pack("<i", 0))
    body += prop("MapName", "NameProperty", s("cs_p"))
    # PlayerStats: one element, itself a nested property list terminated by "None".
    player = prop("Name", "StrProperty", s("Fixture"))
    player += prop("Score", "IntProperty", struct.pack("<i", 500))
    player += s("None")
    body += s("PlayerStats") + s("ArrayProperty")
    body += struct.pack("<i", 0)  # size hint (unused - ArrayProperty is read structurally)
    body += struct.pack("<i", 0)  # array_index for the ArrayProperty itself
    body += struct.pack("<i", 1)  # count
    body += player
    body += s("None")

    game_type = s("TAGame.Replay_Soccar_TA")
    header_payload = struct.pack("<i", 868) + struct.pack("<i", 32) + struct.pack("<i", 10)
    header_payload += game_type + body
    header_size = len(header_payload)
    return struct.pack("<i", header_size) + struct.pack("<I", 0) + header_payload


_RL_SRC = REPO_ROOT / "plugins" / "rl" / "src"
if str(_RL_SRC) not in sys.path:
    sys.path.insert(0, str(_RL_SRC))


def _rl_plugin_dir(
    tmp_path: Path, *, process_name: str, replay_folder: Path, state_path: Path
) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir(exist_ok=True)
    dest = directory / "rl"
    shutil.copytree(REPO_ROOT / "plugins" / "rl", dest)
    manifest_path = dest / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["config"]["detection"]["poll_interval_s"] = 0.2
    data["config"]["detection"]["process_names"] = [process_name]
    data["config"]["replay_folder"] = str(replay_folder)
    data["config"]["calibration_state_path"] = str(state_path)
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
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["rl"], "dir": str(plugins_dir)},
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
    raise AssertionError(f"{plugin_id} is {record.state if record else None!r}")


@pytest.fixture
async def core(tmp_path: Path):
    replay_folder = tmp_path / "replays"
    replay_folder.mkdir()
    state_path = tmp_path / "calibration.json"
    process_name = Path(sys.executable).name  # this test process is always "running"
    plugins_dir = _rl_plugin_dir(
        tmp_path, process_name=process_name, replay_folder=replay_folder, state_path=state_path
    )
    cfg = _config(tmp_path, plugins_dir=plugins_dir)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    core.extensions["rl_replay_folder"] = replay_folder
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def test_rl_plugin_runs_and_registers_its_tools(core: NoxCore) -> None:
    await _wait_state(core, "rl", PluginState.RUNNING)
    for name in ("rl.status.read", "rl.replay.list", "rl.replay.summary", "rl.calibrate"):
        assert core.plugins.tools.get(name) is not None


async def test_fake_game_detection_emits_game_detected(core: NoxCore) -> None:
    # Subscribe *before* waiting for RUNNING: detection is edge-triggered on the very first poll
    # (the test's own process is already "running" at t=0), so it can fire before a subscriber
    # registered only after observing RUNNING would ever see it.
    seen: list[Event] = []
    unsub = core.bus.subscribe(E.GAME_DETECTED, lambda ev: seen.append(ev))
    try:
        await _wait_state(core, "rl", PluginState.RUNNING)
        await asyncio.wait_for(_wait_for(lambda: bool(seen)), timeout=20)
    finally:
        unsub()
    assert seen[0].payload["game"] == "rocket_league"


async def test_new_replay_file_is_parsed_and_emits_rl_replay_parsed(core: NoxCore) -> None:
    await _wait_state(core, "rl", PluginState.RUNNING)
    # Give the watcher time to complete its first `seed_known()` scan of the (empty) folder before
    # the fixture appears, so it is treated as genuinely new.
    await asyncio.sleep(1.0)

    seen: list[Event] = []
    unsub = core.bus.subscribe(E.RL_REPLAY_PARSED, lambda ev: seen.append(ev))
    try:
        fixture_path = core.extensions["rl_replay_folder"] / "fixture.replay"
        fixture_path.write_bytes(_build_fixture_replay())
        await asyncio.wait_for(_wait_for(lambda: bool(seen)), timeout=45)
    finally:
        unsub()
    assert seen[0].payload["parse_status"] == "ok"
    assert seen[0].payload["header"]["map"] == "cs_p"


async def _wait_for(predicate, *, interval: float = 0.2) -> None:
    while not predicate():  # noqa: ASYNC110 - simple test polling helper, no event to wait on
        await asyncio.sleep(interval)
