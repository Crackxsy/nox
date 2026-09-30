"""File tools against a real core: real profile, real permission engine, real filesystem.

Two things are worth checking here that no unit test can. First, that a refusal from the *boundary*
and a refusal from the *permission engine* are different events - a read inside a configured folder
is allowed by the profile and still refused when no folder is configured, and those two must not be
confused for one another. Second, that the capability catalogue noticed: `file.read` used to be a
row in the gap table, and a row left behind after a capability ships is a confident "I cannot" about
something Nox can do.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.security.model import Decision, PermissionRequest
from tests._ports import free_port_base


def _config(tmp_path: Path, roots: list[str]) -> Any:
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
        "files": {"roots": roots},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roots: list[str]) -> Any:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    instance = NoxCore(_config(tmp_path, roots), voice=False)
    await asyncio.wait_for(instance.start(), timeout=60)
    return instance


@pytest.fixture
async def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "arbeit"
    folder.mkdir()
    (folder / "notiz.txt").write_bytes(b"Inhalt der Notiz")
    return folder


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Any:
    instance = await _core(tmp_path, monkeypatch, [str(workspace)])
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


@pytest.fixture
async def core_without_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The default installation: the tools are there and no folder has been configured."""
    instance = await _core(tmp_path, monkeypatch, [])
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


async def _as_model(core: NoxCore, tool: str, /, **arguments: Any) -> Any:
    return await core.tool_executor.call(
        agent="companion", name=tool, arguments=arguments, mode="companion"
    )


# ---- the wiring ------------------------------------------------------------------------------


async def test_the_file_tools_are_registered(core: NoxCore) -> None:
    names = core.tool_registry.names()

    for expected in ("file.roots", "file.list", "file.read", "file.write", "file.move"):
        assert expected in names, expected
    assert "file.delete" in names, "the Recycle Bin is available here, so the tool should be too"


async def test_reading_inside_a_configured_folder_works(core: NoxCore, workspace: Path) -> None:
    result = await _as_model(core, "file.read", path=str(workspace / "notiz.txt"))

    assert result.ok, result.error
    assert result.data["text"] == "Inhalt der Notiz"


async def test_a_listing_shows_the_folder(core: NoxCore, workspace: Path) -> None:
    result = await _as_model(core, "file.list", path=str(workspace))

    assert result.ok, result.error
    assert [entry["name"] for entry in result.data["entries"]] == ["notiz.txt"]


async def test_the_tool_says_which_folders_exist(core: NoxCore, workspace: Path) -> None:
    """So a model can ask instead of guessing a path and being refused."""
    result = await _as_model(core, "file.roots")

    assert result.data["folders"] == [str(workspace)]


# ---- the two different refusals ----------------------------------------------------------------


async def test_a_path_outside_the_folders_is_refused_by_the_boundary(
    core: NoxCore, tmp_path: Path
) -> None:
    result = await _as_model(core, "file.read", path=str(tmp_path / "vault"))

    # Allowed by the profile, refused by the boundary - and the answer says which folders exist.
    assert result.ok, "the permission engine allows reads; the refusal is the boundary's"
    assert result.data["ok"] is False
    assert "outside the folders" in result.data["error"]


async def test_with_no_folder_configured_nothing_is_reachable(
    core_without_folders: NoxCore, tmp_path: Path
) -> None:
    """The inversion that must never happen: an empty list means nowhere, not everywhere."""
    result = await _as_model(core_without_folders, "file.list", path=str(tmp_path))

    assert result.ok
    assert result.data["ok"] is False
    assert "files.roots" in result.data["error"], "the answer has to name the setting to change"


async def test_changing_a_file_asks_the_user_first(core: NoxCore, workspace: Path) -> None:
    """Medium risk, so every profile that has not said otherwise opens a confirmation.

    Asked rather than executed: executing it waits a minute for a dialog nobody is watching.
    """
    spec = core.tool_registry.get("file.write")

    decided = core.security.engine.preview(
        PermissionRequest(
            agent="companion",
            tool="file",
            action="write",
            mode="companion",
            risk=spec.risk,
            target=str(workspace / "neu.txt"),
        )
    )

    assert decided.decision is Decision.CONFIRM
    assert not (workspace / "neu.txt").exists()


async def test_deleting_asks_the_user_first(core: NoxCore, workspace: Path) -> None:
    spec = core.tool_registry.get("file.delete")

    decided = core.security.engine.preview(
        PermissionRequest(
            agent="companion",
            tool="file",
            action="delete",
            mode="companion",
            risk=spec.risk,
            target=str(workspace / "notiz.txt"),
        )
    )

    assert decided.decision is Decision.CONFIRM
    assert (workspace / "notiz.txt").exists()


# ---- what the catalogue now says ----------------------------------------------------------------


async def test_the_catalogue_stopped_calling_files_impossible(core: NoxCore) -> None:
    """A gap row that outlives its capability is a confident "I cannot" about a thing Nox does."""
    result = await _as_model(core, "capabilities.list")
    missing = {gap["name"] for gap in result.data["missing"]}

    assert "file.read" not in missing
    assert "file.write" not in missing
    assert "file.delete" not in missing
    assert "file.read" in result.data["usable"]
