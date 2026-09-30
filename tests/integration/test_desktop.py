"""Desktop tools against a real core - and against the real machine, carefully.

Reading is done for real: this asks the actual window list and the actual process table, because a
fake probe cannot notice a tool that was never registered or a permission that refuses it. Nothing
here closes or ends anything: the destructive tools are checked by asking the permission engine what
it would decide, which is the assertion that matters and does not cost the machine a program.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.security.model import Decision, PermissionRequest
from tests._ports import free_port_base


def _config(tmp_path: Path) -> Any:
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
        "plugins": {"enabled": []},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    instance = NoxCore(_config(tmp_path), voice=False)
    await asyncio.wait_for(instance.start(), timeout=60)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


async def _as_model(core: NoxCore, tool: str, /, **arguments: Any) -> Any:
    return await core.tool_executor.call(
        agent="companion", name=tool, arguments=arguments, mode="companion"
    )


def _decision(core: NoxCore, tool: str, action: str, target: str = "") -> Decision:
    spec = core.tool_registry.get(f"{tool}.{action}")
    assert spec is not None, f"{tool}.{action} is not registered"
    return core.security.engine.preview(
        PermissionRequest(
            agent="companion",
            tool=tool,
            action=action,
            mode="companion",
            risk=spec.risk,
            target=target,
        )
    ).decision


async def test_the_desktop_tools_are_registered(core: NoxCore) -> None:
    names = core.tool_registry.names()

    for expected in (
        "desktop.windows",
        "desktop.processes",
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.window_close",
        "desktop.process_stop",
    ):
        assert expected in names, expected


async def test_the_real_process_table_is_readable(core: NoxCore) -> None:
    result = await _as_model(core, "desktop.processes")

    assert result.ok, result.error
    assert result.data["total"] > 20, "a Windows machine has hundreds of processes"
    assert len(result.data["processes"]) <= 25
    assert all(row["name"] for row in result.data["processes"])
    # Sorted by memory, so a model asking "what is eating my RAM" gets the answer first.
    sizes = [row["memory_mb"] for row in result.data["processes"]]
    assert sizes == sorted(sizes, reverse=True)


async def test_the_real_window_list_is_readable(core: NoxCore) -> None:
    """Every entry has a title and a program, or the listing is not worth sending to a model."""
    result = await _as_model(core, "desktop.windows")

    assert result.ok, result.error
    for window in result.data["windows"]:
        assert window["title"] and window["pid"]
        assert "is_game" in window


async def test_asking_for_a_window_that_is_not_open_is_an_answer(core: NoxCore) -> None:
    result = await _as_model(
        core, "desktop.window_focus", title="ein-fenster-das-es-sicher-nicht-gibt"
    )

    assert result.ok, result.error
    assert result.data["ok"] is False and "no open window matches" in result.data["error"]


async def test_looking_is_free_and_changing_is_not(core: NoxCore) -> None:
    """The levels are the design: reading needs no dialog, closing and ending do."""
    assert _decision(core, "desktop", "windows") is Decision.ALLOW
    assert _decision(core, "desktop", "processes") is Decision.ALLOW
    assert _decision(core, "desktop", "window_focus") is Decision.ALLOW
    assert _decision(core, "desktop", "window_close") is Decision.CONFIRM
    assert _decision(core, "desktop", "process_stop") is Decision.CONFIRM


async def test_ending_nox_itself_is_refused_by_the_boundary(core: NoxCore) -> None:
    """Not through the engine but through the code: a confirmation here would be one question
    too far."""
    from nox.desktop.processes import stop_process

    answer = await stop_process(os.getpid(), game_pids=set())

    assert answer["ok"] is False and "Nox itself" in answer["error"]


async def test_the_catalogue_knows_about_the_new_tools(core: NoxCore) -> None:
    result = await _as_model(core, "capabilities.list")
    usable = set(result.data["usable"])
    missing = {gap["name"] for gap in result.data["missing"]}

    assert "desktop.windows" in usable and "desktop.processes" in usable
    # The rows for these two shipped with the tools and had to come out of the table.
    assert "process.stop" not in missing and "window.manage" not in missing
