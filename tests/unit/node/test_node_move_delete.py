"""`file.move` and `file.delete` on a node (review panel round 1, H4).

Both ends of a move and the file deleted follow the write rules and are decided on handles
(`nox.node.safe_fs.move_file`, `trash_file`). The escapes are tried for real on this OS (junctions
on Windows, symlinks elsewhere) and a swap between the check and the rename is staged by
wrapping the rename itself. A delete goes into the trash or does not happen: the real Recycle
Bin is used only on a CI runner, the freedesktop trash in a temporary home."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nox.node import safe_fs
from nox.node.gate import NodeGate
from nox.node.hands import (
    MoveArgs,
    NodeHands,
    PathArgs,
    delete_refusal,
    hand_tools,
    move_refusal,
)
from nox.node.install import _delete_precheck, _move_precheck, _move_target
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.registry import ROUTABLE
from nox.node.safe_fs import Folders, OutsideFolders, move_file, trash_file
from nox.node.tools import NodeRefusal

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
ON_CI = bool(os.environ.get("GITHUB_ACTIONS"))


def link_folder(target: Path, link: Path) -> None:
    """A link to a folder the way this OS offers one without privileges."""
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "erlaubt"
    (root / "notizen").mkdir(parents=True)
    (root / "archiv").mkdir()
    (root / "notizen" / "einkauf.txt").write_text("Haferflocken\n", encoding="utf-8")
    node = tmp_path / "node-home"
    node.mkdir()
    (node / "node.key").write_text("NODE-KEY", encoding="utf-8")
    outside = tmp_path / "anderswo"
    outside.mkdir()
    (outside / "privat.txt").write_text("not for Nox", encoding="utf-8")
    return {"root": root, "node": node, "outside": outside}


def folders(world: dict[str, Path]) -> Folders:
    return Folders.build([str(world["root"])], [world["node"]])


def reason(work: Callable[[], Any]) -> str:
    with pytest.raises(OutsideFolders) as caught:
        work()
    return caught.value.reason


def hands(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    node_hands = NodeHands(
        folders=lambda: [str(world["root"])], protected=(world["node"],), sandbox=lambda: None
    )
    return hand_tools(node_hands, **kw)


# ---- move ----------------------------------------------------------------------------------------


def test_a_file_is_moved_and_renamed_inside_the_folders(world: dict[str, Path]) -> None:
    f = folders(world)
    moved = move_file(f, "notizen/einkauf.txt", "archiv/einkauf-alt.txt")
    assert (world["root"] / "archiv" / "einkauf-alt.txt").read_text() == "Haferflocken\n"
    assert not (world["root"] / "notizen" / "einkauf.txt").exists()
    assert moved.target.endswith("einkauf-alt.txt")
    # An existing folder as the target keeps the file's name.
    moved = move_file(f, "archiv/einkauf-alt.txt", "notizen")
    assert (world["root"] / "notizen" / "einkauf-alt.txt").exists()


def test_a_move_never_replaces_and_never_creates_folders(world: dict[str, Path]) -> None:
    f = folders(world)
    (world["root"] / "archiv" / "einkauf.txt").write_text("schon da", encoding="utf-8")
    with pytest.raises(FileExistsError):
        move_file(f, "notizen/einkauf.txt", "archiv/einkauf.txt")
    assert (world["root"] / "archiv" / "einkauf.txt").read_text() == "schon da"
    with pytest.raises(FileNotFoundError):
        move_file(f, "notizen/einkauf.txt", "neu/einkauf.txt")
    assert not (world["root"] / "neu").exists()
    assert (world["root"] / "notizen" / "einkauf.txt").exists()


@pytest.mark.parametrize(
    ("target", "why"),
    [
        ("archiv/start.bat", "extension_refused"),
        ("archiv/seite.html", "format_not_writable"),
        ("archiv/.bashrc", "hidden_refused"),
        ("archiv/package.json", "config_refused"),
    ],
)
def test_a_move_cannot_turn_data_into_something_that_runs(
    world: dict[str, Path], target: str, why: str
) -> None:
    f = folders(world)
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", target)) == why
    assert move_refusal("notizen/einkauf.txt", target, overwrite=False, create_folders=False) == why
    assert (world["root"] / "notizen" / "einkauf.txt").exists()


def test_the_source_follows_the_write_rules_too(world: dict[str, Path]) -> None:
    f = folders(world)
    (world["root"] / "notizen" / "werkzeug.exe").write_bytes(b"MZ")
    assert reason(lambda: move_file(f, "notizen/werkzeug.exe", "archiv/werkzeug.txt")) == (
        "extension_refused"
    )
    repo = world["root"] / "projekt"
    (repo / ".git").mkdir(parents=True)
    (repo / "liesmich.txt").write_text("x", encoding="utf-8")
    assert reason(lambda: move_file(f, "projekt/liesmich.txt", "archiv/x.txt")) == (
        "working_tree_refused"
    )
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", "projekt/einkauf.txt")) == (
        "working_tree_refused"
    )


def test_neither_end_leaves_the_allowed_folders(world: dict[str, Path]) -> None:
    f = folders(world)
    outside = str(world["outside"] / "einkauf.txt")
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", outside)) == "outside_folders"
    private = str(world["outside"] / "privat.txt")
    assert reason(lambda: move_file(f, private, "archiv/privat.txt")) == "outside_folders"
    assert reason(lambda: move_file(f, "../node-home/node.key", "archiv/k.txt")) == (
        "path_alias_refused"
    )
    assert (world["outside"] / "privat.txt").exists()


def test_a_linked_folder_at_either_end_is_never_followed(world: dict[str, Path]) -> None:
    f = folders(world)
    link_folder(world["outside"], world["root"] / "tor")
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", "tor/einkauf.txt")) in (
        "link_refused",
        "outside_folders",
    )
    assert reason(lambda: move_file(f, "tor/privat.txt", "archiv/privat.txt")) in (
        "link_refused",
        "outside_folders",
    )
    assert not (world["outside"] / "einkauf.txt").exists()
    assert (world["outside"] / "privat.txt").exists()
    link_folder(world["node"], world["root"] / "knoten")
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", "knoten/einkauf.txt")) in (
        "link_refused",
        "outside_folders",
        "node_home_protected",
    )


@pytest.mark.skipif(WINDOWS, reason="symlinks to files need a privilege on Windows")
def test_a_linked_file_is_not_moved(world: dict[str, Path]) -> None:
    f = folders(world)
    os.symlink(world["outside"] / "privat.txt", world["root"] / "notizen" / "zeiger.txt")
    assert reason(lambda: move_file(f, "notizen/zeiger.txt", "archiv/zeiger.txt")) == (
        "link_refused"
    )


@pytest.mark.skipif(WINDOWS, reason="the POSIX rename is relative to directory fds")
def test_a_target_folder_swapped_for_a_link_after_the_check_changes_nothing(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: once `archiv` was verified, it is replaced by a link to a folder outside. The
    rename is relative to the verified fd, so the file lands in the real (renamed) folder - never
    behind the link."""
    f = folders(world)
    real = safe_fs._rename_noreplace

    def swap_then_rename(*args: Any) -> None:
        os.rename(world["root"] / "archiv", world["root"] / "archiv-echt")
        os.symlink(world["outside"], world["root"] / "archiv", target_is_directory=True)
        real(*args)

    monkeypatch.setattr(safe_fs, "_rename_noreplace", swap_then_rename)
    move_file(f, "notizen/einkauf.txt", "archiv/einkauf.txt")
    assert (world["root"] / "archiv-echt" / "einkauf.txt").exists()
    assert not (world["outside"] / "einkauf.txt").exists()


@pytest.mark.skipif(WINDOWS, reason="the POSIX rename is relative to directory fds")
def test_a_source_swapped_after_the_check_is_moved_back_and_refused(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    f = folders(world)
    real = safe_fs._rename_noreplace
    calls: list[int] = []

    def swap_then_rename(*args: Any) -> None:
        if not calls:
            source = world["root"] / "notizen" / "einkauf.txt"
            os.rename(source, world["root"] / "notizen" / "gesehen.txt")
            source.write_text("untergeschoben", encoding="utf-8")
        calls.append(1)
        real(*args)

    monkeypatch.setattr(safe_fs, "_rename_noreplace", swap_then_rename)
    assert reason(lambda: move_file(f, "notizen/einkauf.txt", "archiv/einkauf.txt")) == "changed"
    assert (world["root"] / "notizen" / "einkauf.txt").read_text() == "untergeschoben"
    assert not (world["root"] / "archiv" / "einkauf.txt").exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX")
def test_without_a_no_replace_rename_a_hard_link_still_never_replaces(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoRename:
        def __getattr__(self, _name: str) -> Any:
            raise AttributeError

    monkeypatch.setattr(safe_fs, "_libc", lambda: NoRename())
    f = folders(world)
    (world["root"] / "archiv" / "einkauf.txt").write_text("schon da", encoding="utf-8")
    with pytest.raises(FileExistsError):
        move_file(f, "notizen/einkauf.txt", "archiv/einkauf.txt")
    move_file(f, "notizen/einkauf.txt", "archiv/neu.txt")
    assert (world["root"] / "archiv" / "neu.txt").read_text() == "Haferflocken\n"
    assert not (world["root"] / "notizen" / "einkauf.txt").exists()


@pytest.mark.skipif(not WINDOWS, reason="Windows handles")
def test_on_windows_the_pinned_target_folder_cannot_be_swapped(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target folder is held without FILE_SHARE_DELETE while the source handle is renamed
    into it: replacing it by a junction in between is refused by Windows itself."""
    f = folders(world)
    real = safe_fs._win_rename
    tried: list[str] = []

    def swap_then_rename(fd: int, new_path: str) -> None:
        if not tried:
            try:
                os.rename(world["root"] / "archiv", world["root"] / "archiv-echt")
                tried.append("renamed")
            except OSError:
                tried.append("pinned")
        real(fd, new_path)

    monkeypatch.setattr(safe_fs, "_win_rename", swap_then_rename)
    move_file(f, "notizen/einkauf.txt", "archiv/einkauf.txt")
    assert tried == ["pinned"]
    assert (world["root"] / "archiv" / "einkauf.txt").exists()


# ---- delete --------------------------------------------------------------------------------------


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "home" / ".local" / "share"
    data.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return data


@pytest.mark.skipif(not LINUX, reason="the freedesktop trash")
def test_a_delete_goes_into_the_freedesktop_trash(
    world: dict[str, Path], xdg_home: Path, tmp_path: Path
) -> None:
    f = folders(world)
    where = trash_file(f, "notizen/einkauf.txt")
    trashed = xdg_home / "Trash" / "files" / "einkauf.txt"
    assert where == str(trashed) and trashed.read_text() == "Haferflocken\n"
    info = (xdg_home / "Trash" / "info" / "einkauf.txt.trashinfo").read_text()
    assert info.startswith("[Trash Info]\nPath=") and "einkauf.txt" in info
    assert "DeletionDate=" in info
    assert not (world["root"] / "notizen" / "einkauf.txt").exists()
    # The same name again: a second entry, never replacing the first.
    (world["root"] / "notizen" / "einkauf.txt").write_text("neu", encoding="utf-8")
    second = trash_file(f, "notizen/einkauf.txt")
    assert second.endswith("einkauf.2.txt") and trashed.read_text() == "Haferflocken\n"


@pytest.mark.skipif(not LINUX, reason="the freedesktop trash")
def test_a_file_swapped_before_the_trash_rename_is_put_back(
    world: dict[str, Path], xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = folders(world)
    real = safe_fs._rename_noreplace
    calls: list[int] = []

    def swap_then_rename(*args: Any) -> None:
        if not calls:
            source = world["root"] / "notizen" / "einkauf.txt"
            os.rename(source, world["root"] / "notizen" / "gesehen.txt")
            source.write_text("untergeschoben", encoding="utf-8")
        calls.append(1)
        real(*args)

    monkeypatch.setattr(safe_fs, "_rename_noreplace", swap_then_rename)
    assert reason(lambda: trash_file(f, "notizen/einkauf.txt")) == "changed"
    assert (world["root"] / "notizen" / "einkauf.txt").read_text() == "untergeschoben"
    assert not list((xdg_home / "Trash" / "info").iterdir())


@pytest.mark.skipif(WINDOWS, reason="POSIX links")
def test_a_linked_file_or_folder_is_never_deleted_through(
    world: dict[str, Path], xdg_home: Path
) -> None:
    f = folders(world)
    os.symlink(world["outside"] / "privat.txt", world["root"] / "notizen" / "zeiger.txt")
    assert reason(lambda: trash_file(f, "notizen/zeiger.txt")) == "link_refused"
    link_folder(world["outside"], world["root"] / "tor")
    assert reason(lambda: trash_file(f, "tor/privat.txt")) == "link_refused"
    assert (world["outside"] / "privat.txt").exists()


def test_a_delete_follows_the_write_rules(world: dict[str, Path]) -> None:
    f = folders(world)
    (world["root"] / "notizen" / "werkzeug.exe").write_bytes(b"MZ")
    assert reason(lambda: trash_file(f, "notizen/werkzeug.exe")) == "extension_refused"
    assert delete_refusal("notizen/werkzeug.exe") == "extension_refused"
    assert reason(lambda: trash_file(f, str(world["outside"] / "privat.txt"))) == (
        "outside_folders"
    )
    assert reason(lambda: trash_file(f, "notizen")) in ("extension_refused", "not_a_file")
    assert (world["root"] / "notizen" / "werkzeug.exe").exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX")
def test_a_file_on_another_drive_than_the_trash_is_refused(
    world: dict[str, Path], xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS uses the system's Trash")
    f = folders(world)
    real_fstat = os.fstat
    trash = str(xdg_home / "Trash" / "files")

    def other_drive(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if safe_fs._final_path(fd) == trash:
            values = list(info)
            values[2] = info.st_dev + 1  # st_dev: the trash is on another drive
            return os.stat_result(values)
        return info

    monkeypatch.setattr(safe_fs.os, "fstat", other_drive)
    assert reason(lambda: trash_file(f, "notizen/einkauf.txt")) == "trash_unavailable"
    assert (world["root"] / "notizen" / "einkauf.txt").exists()


@pytest.mark.skipif(not WINDOWS, reason="the Recycle Bin")
def test_on_windows_the_file_must_end_up_in_the_recycle_bin(
    world: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell call is replaced by a move into a folder named like the bin, so this runs
    without touching the real Recycle Bin. A shell that moved something else (the file was
    swapped) or deleted the file for good is reported, never claimed as recycled."""
    import nox.files.recycle as recycle

    f = folders(world)
    fake_bin = world["root"] / "$Recycle.Bin" / "S-1-5-21-0"
    fake_bin.mkdir(parents=True)
    monkeypatch.setattr(safe_fs, "_win_bin_refusal", lambda _final, _size: None)
    monkeypatch.setattr(
        recycle, "to_recycle_bin", lambda path: os.rename(path, fake_bin / "$RAB12CD.txt")
    )
    where = trash_file(f, "notizen/einkauf.txt")
    assert "\\$RECYCLE.BIN\\" in where.upper()
    assert not (world["root"] / "notizen" / "einkauf.txt").exists()

    (world["root"] / "notizen" / "zwei.txt").write_text("zwei", encoding="utf-8")

    def swapped(path: Path) -> None:
        os.rename(path, world["root"] / "archiv" / "zwei.txt")  # the checked file went elsewhere
        (world["root"] / "notizen" / "fremd.txt").write_text("x", encoding="utf-8")
        os.rename(world["root"] / "notizen" / "fremd.txt", fake_bin / "$REF34GH.txt")

    monkeypatch.setattr(recycle, "to_recycle_bin", swapped)
    assert reason(lambda: trash_file(f, "notizen/zwei.txt")) == "changed"

    (world["root"] / "notizen" / "drei.txt").write_text("drei", encoding="utf-8")
    monkeypatch.setattr(recycle, "to_recycle_bin", lambda path: os.remove(path))
    assert reason(lambda: trash_file(f, "notizen/drei.txt")) == "trash_failed"


@pytest.mark.skipif(not WINDOWS, reason="the Recycle Bin")
def test_on_windows_a_bin_that_would_not_keep_the_file_refuses(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import nox.files.recycle as recycle

    called: list[Path] = []
    monkeypatch.setattr(recycle, "to_recycle_bin", called.append)
    monkeypatch.setattr(
        safe_fs, "_win_bin_refusal", lambda _final, _size: "the drive has no Recycle Bin"
    )
    assert reason(lambda: trash_file(folders(world), "notizen/einkauf.txt")) == (
        "trash_unavailable"
    )
    assert called == [] and (world["root"] / "notizen" / "einkauf.txt").exists()
    # Read-only on the real machine: the drive of the test folder is a fixed one with a bin.
    assert safe_fs._win_bin_refusal(str(world["root"]), 10) in (None, *_KNOWN_BIN_REASONS)


_KNOWN_BIN_REASONS = (
    "the Recycle Bin is switched off by policy",
    "the Recycle Bin of this drive deletes files immediately",
    "the drive has no Recycle Bin",
)


@pytest.mark.skipif(not (WINDOWS and ON_CI), reason="the real Recycle Bin, on a CI runner only")
def test_on_a_ci_runner_the_real_recycle_bin_takes_the_file(world: dict[str, Path]) -> None:
    where = trash_file(folders(world), "notizen/einkauf.txt")
    assert "\\$RECYCLE.BIN\\" in where.upper()
    assert not (world["root"] / "notizen" / "einkauf.txt").exists()


# ---- the tools -----------------------------------------------------------------------------------


async def test_the_tools_are_high_never_replace_and_say_what_they_do(
    world: dict[str, Path], xdg_home: Path
) -> None:
    tools = hands(world, platform="linux", trash=lambda: None)
    move, delete = tools["file.move"], tools["file.delete"]
    assert move.risk == delete.risk == "high" and move.group == delete.group == "files"
    assert move.path_safe and delete.path_safe
    args = MoveArgs(path="notizen/einkauf.txt", to="archiv/alt.txt")
    assert move.shown is not None and move.shown(args) == {
        "move": "notizen/einkauf.txt",
        "to": "archiv/alt.txt",
        "replaces": "never",
    }
    assert move.precheck is not None
    assert move.precheck(MoveArgs(path="a.txt", to="b.txt", overwrite=True)) == "overwrite_refused"
    assert move.precheck(MoveArgs(path="a.txt", to="b.txt", create_folders=True)) == (
        "folders_not_created"
    )
    with pytest.raises(NodeRefusal) as refused:
        await move.handler(MoveArgs(path="notizen/einkauf.txt", to="archiv/x", overwrite=True))
    assert refused.value.reason == "overwrite_refused"
    answer = await move.handler(args)
    assert answer["ok"] is True and answer["to"].endswith("alt.txt")
    exists = await move.handler(MoveArgs(path="archiv/alt.txt", to="archiv/alt.txt"))
    assert exists["ok"] is False
    if LINUX:
        gone = await delete.handler(PathArgs(path="archiv/alt.txt"))
        assert gone["ok"] is True and gone["recoverable"] is True
    with pytest.raises(NodeRefusal) as outside:
        await delete.handler(PathArgs(path=str(world["outside"] / "privat.txt")))
    assert outside.value.reason == "outside_folders"


def test_both_are_offered_only_with_files_and_withheld_where_they_cannot_work(
    world: dict[str, Path],
) -> None:
    assert {"file.move", "file.delete"} <= ROUTABLE
    tools = hands(world, platform="linux", trash=lambda: None)
    gate = NodeGate(tools, NodeManifest(), None, protected=world["node"])
    assert not {"file.move", "file.delete"} & {t.name for t in gate.offered()}
    gate.set_manifest(NodeManifest(groups={g: g == "files" for g in GROUPS}))
    assert {"file.move", "file.delete"} <= {t.name for t in gate.offered()}
    # No trash this device can use: no delete offered at all (never claimed, never an unlink).
    assert "file.delete" not in hands(world, platform="linux", trash=lambda: "no trash")
    # macOS has no sandbox backend: `shell.run` is not offered there.
    assert "shell.run" not in hands(world, platform="darwin", trash=lambda: None)
    assert "shell.run" in hands(world, platform="linux", trash=lambda: None)


def test_the_core_shows_a_move_in_full_and_refuses_before_asking() -> None:
    target = _move_target({"path": "notizen/a.txt", "to": "archiv/b.txt"})
    assert target == 'move "notizen/a.txt" to "archiv/b.txt" replaces never'
    assert _move_precheck({"path": "a.txt", "to": "b.bat"}) == "extension_refused"
    assert _move_precheck({"path": "a.txt", "to": "b.txt", "overwrite": True}) == (
        "overwrite_refused"
    )
    assert _move_precheck({"path": "a.txt", "to": "archiv"}) is None  # a folder to move into
    assert _delete_precheck({"path": "start.ps1"}) == "extension_refused"
    assert _delete_precheck({"path": "notizen/a.txt"}) is None
