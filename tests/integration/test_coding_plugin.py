"""Coding plugin, end to end: a real `NoxCore` (headless) in the `coding` profile spawns the real
`nox_plugin_coding` worker subprocess against a fake `claude` executable (`.cmd` wrapper around
`tests/unit/plugins/coding/fake_claude.py`, resolved via `shutil.which` exactly like production).
Modelled on `tests/integration/test_obs_plugin.py`."""

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

pytestmark = pytest.mark.timeout(180)

FAKE_CLAUDE_SCRIPT = REPO_ROOT / "tests" / "unit" / "plugins" / "coding" / "fake_claude.py"


def _fake_claude_wrapper(tmp_path: Path) -> Path:
    """A `.cmd` wrapper so `shutil.which` resolution (an absolute path ending in a `PATHEXT`
    extension) finds a real, spawnable executable on Windows without touching PATH."""
    wrapper = tmp_path / "fake_claude.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{FAKE_CLAUDE_SCRIPT}" %*\r\n', encoding="utf-8"
    )
    return wrapper


def _coding_plugin_dir(tmp_path: Path, *, command: Path, workspace: Path) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    dest = directory / "coding"
    shutil.copytree(REPO_ROOT / "plugins" / "coding", dest)
    manifest_path = dest / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["config"]["command"] = str(command)
    data["config"]["filesystem_roots"] = [str(workspace)]
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
        "security": {"profile": "coding"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["coding"], "dir": str(plugins_dir)},
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


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("line one\n", encoding="utf-8")
    return ws


@pytest.fixture
async def core(tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CLI_MODE", "ok")
    command = _fake_claude_wrapper(tmp_path)
    plugins_dir = _coding_plugin_dir(tmp_path, command=command, workspace=workspace)
    cfg = _config(tmp_path, plugins_dir=plugins_dir)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def test_coding_plugin_runs_in_the_coding_profile_and_registers_its_tools(
    core: NoxCore,
) -> None:
    await _wait_state(core, "coding", PluginState.RUNNING)
    records = core.plugins.records()
    assert records["coding"].process is not None and records["coding"].process.poll() is None
    for name in (
        "coding.session.start",
        "coding.session.status.read",
        "coding.session.stop",
        "coding.review.request",
    ):
        assert core.plugins.tools.get(name) is not None


async def test_coding_session_start_is_forwarded_to_the_worker_and_back(
    core: NoxCore, workspace: Path
) -> None:
    await _wait_state(core, "coding", PluginState.RUNNING)
    spec = core.plugins.tools.get("coding.session.start")
    assert spec is not None and spec.risk == "medium"
    result = await asyncio.wait_for(
        spec.handler({"target": str(workspace), "prompt": "add a line to notes.txt"}), timeout=60
    )
    assert result["outcome"] == "ended"
    assert result["files_touched"] == ["notes.txt"]


async def test_coding_plugin_never_registers_under_the_work_profile(tmp_path: Path) -> None:
    """Structural guarantee (manifest `profiles: [coding]`): the plugin manager refuses to spawn
    the worker at all when the active profile is `work` - never a live process, never a refused
    tool call after the fact."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = _fake_claude_wrapper(tmp_path)
    plugins_dir = _coding_plugin_dir(tmp_path, command=command, workspace=workspace)
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
        "security": {"profile": "work"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": ["coding"], "dir": str(plugins_dir)},
    }
    cfg = load_config(DEFAULTS_PATH, None, None, overrides)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        record = core.plugins.records().get("coding")
        assert record is not None
        assert record.state is not PluginState.RUNNING
        assert record.process is None
        assert core.plugins.tools.get("coding.session.start") is None
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)
