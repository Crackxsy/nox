"""`nox.node.device.safe_fs` for reads, listings and the folders themselves: the text of a path is
checked first, then the file is opened, then the verdict is taken on the open handle - so
links, junctions, hard links and a swap between check and use change nothing. Broad
folders are never allowed and the node folder stays closed."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from nox.node.device.hands import NodeHands, PathArgs, WriteArgs
from nox.node.device.home import NodeHome
from nox.node.device.safe_fs import (
    Folders,
    OutsideFolders,
    list_folder,
    open_existing,
    open_new,
    secret_locations,
)
from nox.node.device.tools import NodeRefusal
from tests.unit.node.support_hands import (
    WINDOWS,
    FakeSandbox,
    folders,
    hand_tools_for,
    link_folder,
    read_file,
    read_refusal,
)

# ---- the text ------------------------------------------------------------------------------------


def test_inside_an_allowed_folder_relative_and_absolute(fs_world: dict[str, Path]) -> None:
    f = folders(fs_world)
    assert read_file(f, "notes/einkauf.txt") == b"Haferflocken\n"
    assert read_file(f, str(fs_world["root"] / "notes" / "einkauf.txt")) == b"Haferflocken\n"


@pytest.mark.parametrize(
    "text",
    [
        "../node-home/node.key",
        "notes/../../node-home/node.key",
        "~/x.txt",
        "NOXNOD~1/node.key",
        "\\\\fileserver\\share\\x.txt",
        "//fileserver/share/x.txt",
        "\\\\?\\C:\\Windows\\win.ini",
        "\\\\.\\pipe\\nox",
        "notes/a\x00b",
    ],
)
def test_aliases_network_and_device_paths_are_refused_unresolved(
    fs_world: dict[str, Path], text: str
) -> None:
    assert read_refusal(folders(fs_world), text) in ("path_alias_refused", "invalid_path")


@pytest.mark.skipif(not WINDOWS, reason="Windows names")
@pytest.mark.parametrize("text", ["notes/a.txt:hidden", "CON", "notes/nul.txt", "notes/x.", "C:x"])
def test_windows_streams_devices_and_ambiguous_names_are_refused(
    fs_world: dict[str, Path], text: str
) -> None:
    assert read_refusal(folders(fs_world), text) == "invalid_path"


@pytest.mark.skipif(not WINDOWS, reason="Windows long paths")
def test_a_resolved_long_path_is_compared_without_its_prefix(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device.safe_fs import _plain

    assert _plain("\\\\?\\C:\\data") == "C:\\data"
    assert _plain("\\\\?\\UNC\\fileserver\\share") == "\\\\?\\UNC\\fileserver\\share"
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p: "\\\\?\\" + real(p))
    long_form = Folders.build([str(fs_world["root"])], [fs_world["node"]])
    assert read_file(long_form, "notes/einkauf.txt") == b"Haferflocken\n"


def test_an_absolute_path_outside_and_no_folder_at_all(fs_world: dict[str, Path]) -> None:
    assert (
        read_refusal(folders(fs_world), str(fs_world["outside"] / "private.txt"))
        == "outside_folders"
    )
    assert read_refusal(Folders.build([], [fs_world["node"]]), "notes/einkauf.txt") == "no_folders"


def test_a_folder_inside_the_node_folder_never_counts(fs_world: dict[str, Path]) -> None:
    inner = fs_world["node"] / "inner"
    inner.mkdir()
    assert Folders.build([str(inner)], [fs_world["node"]]).roots == ()


# ---- the open, on the handle ---------------------------------------------------------------------


def test_a_link_to_the_node_folder_is_refused(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["node"], fs_world["root"] / "door")
    reason = read_refusal(folders(fs_world), "door/node.key")
    assert reason in ("node_home_protected", "link_refused")


def test_a_link_out_of_the_allowed_folder_is_refused(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["outside"], fs_world["root"] / "out")
    assert read_refusal(folders(fs_world), "out/private.txt") in ("outside_folders", "link_refused")


def test_a_new_file_through_a_link_is_never_left_behind(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["node"], fs_world["root"] / "door")
    with pytest.raises(OutsideFolders):
        open_new(folders(fs_world), "door/planted.txt")
    assert not (fs_world["node"] / "planted.txt").exists()


def test_a_hard_link_to_a_protected_file_is_refused(fs_world: dict[str, Path]) -> None:
    os.link(fs_world["node"] / "node.key", fs_world["root"] / "copy.key")
    assert read_refusal(folders(fs_world), "copy.key") == "node_home_protected"


def test_the_node_folder_inside_an_allowed_folder_stays_closed(fs_world: dict[str, Path]) -> None:
    parent = fs_world["root"].parent  # contains both the allowed folder and the node folder
    f = Folders.build([str(parent)], [fs_world["node"]])
    assert read_file(f, "allowed/notes/einkauf.txt") == b"Haferflocken\n"
    assert read_refusal(f, "node-home/node.key") == "node_home_protected"
    with pytest.raises(OutsideFolders):
        with list_folder(f, "node-home"):
            pass


def test_keyring_folders_are_protected(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = fs_world["root"].parent / "appdata"
    monkeypatch.setenv("APPDATA", str(data))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    store = data / ("Microsoft/Credentials" if WINDOWS else "keyrings")
    store.mkdir(parents=True)
    (store / "login.keyring").write_text("x", encoding="utf-8")
    # A link into the store from an allowed folder is refused on the handle.
    link_folder(store, fs_world["root"] / "door")
    hands = NodeHands(
        folders=lambda: [str(fs_world["root"])], protected=(fs_world["node"],), sandbox=lambda: None
    )
    reason = read_refusal(hands.resolved(), "door/login.keyring")
    assert reason in ("node_home_protected", "link_refused")
    if WINDOWS:  # a folder that contains the app-data root is too broad to allow at all
        assert Folders.build([str(data.parent)], [fs_world["node"]]).roots == ()
    for place in (".ssh", ".aws", ".kube", ".git-credentials", ".netrc", ".pgpass"):
        assert Path.home() / place in secret_locations()


def test_a_swap_between_the_text_check_and_the_open_is_caught(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    f = folders(fs_world)
    real_split = Folders.split

    def split_then_swap(self: Folders, text: str) -> Any:
        found = real_split(self, text)
        # The text was fine: `notes` was a real folder. Now it becomes a door to the node folder.
        os.rename(fs_world["root"] / "notes", fs_world["root"] / "notes-old")
        link_folder(fs_world["node"], fs_world["root"] / "notes")
        (fs_world["node"] / "einkauf.txt").write_text("NODE-SECRET", encoding="utf-8")
        return found

    monkeypatch.setattr(Folders, "split", split_then_swap)
    with pytest.raises(OutsideFolders) as caught:
        read_file(f, "notes/einkauf.txt")
    assert caught.value.reason in ("node_home_protected", "link_refused")


def test_after_the_open_the_handle_is_what_counts(fs_world: dict[str, Path]) -> None:
    fd = open_existing(folders(fs_world), "notes/einkauf.txt")
    try:
        target = fs_world["root"] / "notes" / "einkauf.txt"
        if WINDOWS:
            with pytest.raises(PermissionError):  # opened without FILE_SHARE_DELETE
                os.replace(fs_world["outside"] / "private.txt", target)
        else:
            os.replace(fs_world["outside"] / "private.txt", target)
        assert os.read(fd, 100) == b"Haferflocken\n"
    finally:
        os.close(fd)


def test_a_listing_never_follows_links(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["outside"], fs_world["root"] / "out")
    with list_folder(folders(fs_world), ".") as entries:
        names = {e.name: e.is_symlink() or e.is_junction() for e in entries}
    assert set(names) == {"notes", "out"}


async def test_a_listing_reports_junctions_and_links_as_links(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["outside"], fs_world["root"] / "out")
    listing = await hand_tools_for(fs_world)["file.list"].handler(PathArgs(path="."))
    kinds = {e["name"]: e["kind"] for e in listing["entries"]}
    assert kinds == {"notes": "folder", "out": "link"}


async def test_write_never_overwrites_unless_asked_and_never_makes_folders(
    fs_world: dict[str, Path],
) -> None:
    tools = hand_tools_for(fs_world)
    write = tools["file.write"].handler
    new = await write(WriteArgs(path="notes/neu.txt", text="eins"))
    assert new["ok"] is True
    again = await write(WriteArgs(path="notes/neu.txt", text="zwei"))
    assert again["ok"] is False
    await write(WriteArgs(path="notes/neu.txt", text="+", append=True))
    assert (fs_world["root"] / "notes" / "neu.txt").read_text(encoding="utf-8") == "eins+"
    await write(WriteArgs(path="notes/neu.txt", text="drei", overwrite=True))
    assert (fs_world["root"] / "notes" / "neu.txt").read_text(encoding="utf-8") == "drei"
    missing = await write(WriteArgs(path="nowhere/x.txt", text="x"))
    assert missing["ok"] is False and not (fs_world["root"] / "nowhere").exists()


async def test_an_overwrite_through_a_link_truncates_nothing(fs_world: dict[str, Path]) -> None:
    link_folder(fs_world["node"], fs_world["root"] / "door")
    with pytest.raises(NodeRefusal):
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="door/node.key", text="", overwrite=True)
        )
    assert (fs_world["node"] / "node.key").read_text(encoding="utf-8") == "NODE-KEY"


async def test_read_and_list_answer_as_data(fs_world: dict[str, Path]) -> None:
    tools = hand_tools_for(fs_world)
    text = await tools["file.read"].handler(PathArgs(path="notes/einkauf.txt"))
    assert text["text"] == "Haferflocken\n"
    listing = await tools["file.list"].handler(PathArgs(path="notes"))
    assert [e["name"] for e in listing["entries"]] == ["einkauf.txt"]
    with pytest.raises(NodeRefusal) as caught:
        await tools["file.read"].handler(PathArgs(path="../node-home/node.key"))
    assert caught.value.reason == "path_alias_refused"


# ---- allowing a folder: only on the device -------------------------------------------------------


def test_a_folder_is_allowed_only_in_a_terminal_here_and_never_the_node_folder(
    tmp_path: Path,
) -> None:
    from nox.node.device import cli as node_cli
    from nox.node.wire.manifest import load_manifest

    home = NodeHome(tmp_path / "h")
    docs = tmp_path / "Dokumente"
    docs.mkdir()
    said: list[str] = []
    yes = {"interactive": lambda: True, "confirm": lambda _p: True}
    with pytest.raises(node_cli.NodeCliError):  # not from a non-interactive caller (the core)
        node_cli.allow(home, "files", True, say=said.append, folder=str(docs))
    node_cli.allow(home, "files", True, say=said.append, folder=str(docs), **yes)  # type: ignore[arg-type]
    assert load_manifest(home.manifest_file).folders == [os.path.realpath(docs)]
    for bad in (str(home.path), str(home.path / "sub"), "relative", r"\\fileserver\share"):
        with pytest.raises(node_cli.NodeCliError):
            node_cli.allow(home, "files", True, say=said.append, folder=bad, **yes)  # type: ignore[arg-type]
    with pytest.raises(node_cli.NodeCliError):
        node_cli.allow(home, "shell", True, say=said.append, folder=str(docs), **yes)  # type: ignore[arg-type]
    node_cli.allow(home, "files", False, say=said.append, folder=str(docs))
    assert load_manifest(home.manifest_file).folders == []


def test_broad_folders_are_never_allowed(fs_world: dict[str, Path], tmp_path: Path) -> None:
    from nox.node.device import cli as node_cli
    from nox.node.device.safe_fs import broad_folder

    system = [os.environ["SystemRoot"], os.environ["ProgramFiles"]] if WINDOWS else ["/etc", "/usr"]
    for place in [Path.home().anchor, str(Path.home()), str(Path.home().parent), *system]:
        assert broad_folder(place), place
        assert Folders.build([place], [fs_world["node"]]).roots == ()
    assert not broad_folder(str(fs_world["root"]))
    yes = {"interactive": lambda: True, "confirm": lambda _p: True}
    with pytest.raises(node_cli.NodeCliError):
        node_cli.allow(  # type: ignore[arg-type]
            NodeHome(tmp_path / "h"),
            "files",
            True,
            say=lambda _t: None,
            folder=str(Path.home()),
            **yes,
        )


def test_a_link_into_a_secret_store_is_refused(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fs_world["root"].parent / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    link_folder(home / ".ssh", fs_world["root"] / "keys")
    hands = NodeHands(
        folders=lambda: [str(fs_world["root"])], protected=(fs_world["node"],), sandbox=lambda: None
    )
    assert read_refusal(hands.resolved(), "keys/id_ed25519") in (
        "node_home_protected",
        "link_refused",
    )


@pytest.mark.skipif(WINDOWS, reason="FIFOs")
def test_a_fifo_is_refused_without_blocking(fs_world: dict[str, Path]) -> None:
    os.mkfifo(fs_world["root"] / "pipe")
    assert read_refusal(folders(fs_world), "pipe") == "not_a_file"


@pytest.mark.skipif(not WINDOWS, reason="the Windows listing")
def test_a_windows_listing_whose_folder_changed_is_refused(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device import safe_fs

    monkeypatch.setattr(safe_fs, "_win_final", lambda _fd: str(fs_world["outside"]))
    with pytest.raises(OutsideFolders) as caught:
        with list_folder(folders(fs_world), "notes"):
            pass
    assert caught.value.reason == "changed"


async def test_a_huge_listing_stops_counting(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device import hands as hands_module

    monkeypatch.setattr(hands_module, "MAX_SCAN", 3)
    for i in range(6):
        (fs_world["root"] / "notes" / f"f{i}.txt").write_text("x", encoding="utf-8")
    listing = await hand_tools_for(fs_world)["file.list"].handler(PathArgs(path="notes"))
    assert listing["total"] == 3 and listing["stopped_counting"] and listing["truncated"]


def test_utf8_cut_by_the_limit_stays_utf8() -> None:
    from nox.node.device.hands import _decode

    cut = ("aaa" + "é").encode("utf-8")[:-1]
    assert _decode(cut, True) == ("aaa", "utf-8")
    assert _decode(b"caf\xe9 au lait", False)[1] == "cp1252"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap's read-only binds")
async def test_a_node_folder_under_a_system_bind_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.sandbox import linux

    monkeypatch.setattr(linux, "visible_system_paths", lambda: (str(tmp_path),))
    hands = NodeHands(
        folders=lambda: [], protected=(tmp_path / "node",), sandbox=lambda: FakeSandbox()
    )
    assert hands.exposed() is not None
    with pytest.raises(NodeRefusal) as caught:
        await hands.confined()
    assert caught.value.reason == "sandbox_unavailable"


@pytest.mark.skipif(not WINDOWS, reason="the Windows ACL")
def test_the_owner_only_acl_is_put_back_on_every_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device import cli as node_cli

    calls: list[Path] = []
    monkeypatch.setattr(node_cli, "restrict_dir_to_owner", lambda p: calls.append(p) or True)
    home = NodeHome(tmp_path / "h")
    home.ensure()
    for _ in range(2):
        node_cli.build(home, sandbox=lambda: None).audit._conn.close()  # type: ignore[attr-defined]
    assert calls == [home.path, home.path]


def test_the_protected_identities_are_reused_for_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device import safe_fs

    secret = tmp_path / "geheim"
    secret.mkdir()
    walked: list[Path] = []
    real = safe_fs._identities
    monkeypatch.setattr(safe_fs, "_identities", lambda f: walked.append(f) or real(f))
    for _ in range(3):
        Folders.build([], [secret])
    assert walked == [secret]
    monkeypatch.setattr(safe_fs, "PROTECTED_TTL_S", 0.0)
    Folders.build([], [secret])
    assert len(walked) == 2


def test_known_secret_files_are_found_before_the_walk_runs_out(fs_world: dict[str, Path]) -> None:
    profile = fs_world["root"].parent / "browser"
    (profile / "Default").mkdir(parents=True)
    for i in range(600):  # more than the walk looks at
        (profile / f"cache{i:03}").write_text("", encoding="utf-8")
    (profile / "Default" / "Login Data").write_text("PASSWORDS", encoding="utf-8")
    os.link(profile / "Default" / "Login Data", fs_world["root"] / "harmlos.txt")
    f = Folders.build([str(fs_world["root"])], [fs_world["node"], profile])
    assert read_refusal(f, "harmlos.txt") == "node_home_protected"


def test_noxs_own_folder_is_protected_on_a_node() -> None:
    from nox.paths import nox_home

    hands = NodeHands(folders=lambda: [], protected=(), sandbox=lambda: None)
    assert os.path.realpath(nox_home()) in hands.resolved().protected
