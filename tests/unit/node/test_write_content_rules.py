"""What `file.write` on a node may write: an allowlist of plain data files, never an
execution trigger, an executable, a hidden or configuration file, nothing inside a working
tree, and structured formats only whole and only when they parse plainly."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from nox.node.device.gate import NodeGate
from nox.node.device.hands.args import WriteArgs
from nox.node.device.hands.catalogue import hand_tools
from nox.node.device.hands.node_hands import NodeHands
from nox.node.device.tools import NodeRefusal
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.security.model import Risk
from tests.unit.node.support import signed
from tests.unit.node.support_hands import (
    WINDOWS,
    folders,
    hand_tools_for,
    link_folder,
    read_refusal,
)

# ---- what a write may never do -------------------------------------------------------------------

REFUSED_WRITES = ("execution_trigger", "hidden_refused", "extension_refused", "config_refused")


@pytest.mark.parametrize(
    "target",
    [
        ".git/hooks/pre-commit",
        "repo/.GIT/config",
        "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/hello.txt",
        ".bashrc",
        ".zprofile",
        ".config/autostart/nox.desktop",
        ".config/systemd/user/nox.service",
        "Library/LaunchAgents/example.invalid.plist",
        "Documents/WindowsPowerShell/profile.txt",
        "notes/run.ps1",
        "notes/RUN.CMD",
        "notes/start.sh",
        "notes/link.lnk",
        "notes/tool.exe",
    ],
)
async def test_a_write_never_plants_an_execution_trigger(
    fs_world: dict[str, Path], target: str
) -> None:
    folder = (fs_world["root"] / target).parent
    folder.mkdir(parents=True, exist_ok=True)
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text="echo hi"))
    assert caught.value.reason in REFUSED_WRITES
    assert not (fs_world["root"] / target).exists()


async def test_a_link_into_a_git_folder_is_no_way_around(fs_world: dict[str, Path]) -> None:
    hooks = fs_world["root"] / "repo" / ".git" / "hooks"
    hooks.mkdir(parents=True)
    link_folder(hooks, fs_world["root"] / "innocent")
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="innocent/pre-commit", text="x")
        )
    assert caught.value.reason in (*REFUSED_WRITES, "link_refused")
    assert not (hooks / "pre-commit").exists()


@pytest.mark.skipif(WINDOWS, reason="the executable bit")
async def test_an_executable_is_never_written_and_a_new_file_never_executable(
    fs_world: dict[str, Path],
) -> None:
    script = fs_world["root"] / "notes" / "tool.txt"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="notes/tool.txt", text="x", overwrite=True)
        )
    assert caught.value.reason == "executable_refused"
    await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path="notes/neu.txt", text="x"))
    assert (fs_world["root"] / "notes" / "neu.txt").stat().st_mode & 0o111 == 0


async def test_a_file_with_a_second_name_is_not_written(fs_world: dict[str, Path]) -> None:
    os.link(fs_world["root"] / "notes" / "einkauf.txt", fs_world["root"] / "zweit.txt")
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="zweit.txt", text="", overwrite=True)
        )
    assert caught.value.reason == "hard_link_refused"
    assert (fs_world["root"] / "notes" / "einkauf.txt").read_bytes() == b"Haferflocken\n"


# ---- writes are an allowlist of data files ----------------------------------------------------


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (".gitconfig.txt", "hidden_refused"),
        ("notes/.vscode/notes.md", "hidden_refused"),
        ("notes/.hidden/a.txt", "hidden_refused"),
        ("notes/Makefile", "extension_refused"),
        ("notes/README", "extension_refused"),
        ("notes/page.svg", "format_not_writable"),
        ("notes/archive.zip", "extension_refused"),
        ("notes/config.ini", "extension_refused"),
        ("notes/pip.conf", "extension_refused"),
        ("notes/settings.json", "config_refused"),
        ("notes/TASKS.JSON", "config_refused"),
        ("notes/package.json", "config_refused"),
        ("notes/docker-compose.yml", "config_refused"),
        ("notes/appsettings.Development.json", "config_refused"),
        ("notes/workflows/build.yml", "config_refused"),
    ],
)
async def test_only_plain_data_files_are_written(
    fs_world: dict[str, Path], target: str, reason: str
) -> None:
    (fs_world["root"] / target).parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text="x"))
    assert caught.value.reason == reason
    assert not (fs_world["root"] / target).exists()


@pytest.mark.parametrize("target", ["notes/liste.TXT", "notes/plan.md", "notes/daten.csv"])
async def test_data_files_are_written(fs_world: dict[str, Path], target: str) -> None:
    answer = await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text="x"))
    assert answer["ok"] is True


async def test_a_link_cannot_turn_a_data_name_into_a_hidden_folder(
    fs_world: dict[str, Path],
) -> None:
    hidden = fs_world["root"] / "notes" / ".config"
    hidden.mkdir()
    link_folder(hidden, fs_world["root"] / "visible")
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="visible/a.txt", text="x")
        )
    assert caught.value.reason in ("hidden_refused", "link_refused")
    assert not (hidden / "a.txt").exists()


def test_structured_formats_are_always_high_and_passive_ones_medium_when_new() -> None:
    from nox.node.device.hands.args import write_risk
    from nox.node.hub.install.routes import write_risk_of

    for name in ("a.json", "a.YAML", "a.yml"):
        assert write_risk(name, False, False) == "high", name
        assert write_risk_of({"path": f"notes/{name}"}) is Risk.HIGH
    for name in ("a.txt", "a.md", "a.csv", "a.tsv", "a.log", "a.ics", "a.vcf", "a.srt"):
        assert write_risk(name, False, False) == "medium", name
        assert write_risk(name, True, False) == "high"


async def test_a_new_structured_file_needs_a_high_receipt(tmp_path: Path) -> None:
    from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    tools = hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None))
    gate = NodeGate(
        tools, NodeManifest(groups={g: g == "files" for g in GROUPS}), None, receipts=verifier
    )
    args = {"path": "plan.json", "text": "{}", "append": False, "overwrite": False}
    meta = signed(signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64)
    assert gate.check("file.write", args, meta=NoxCallMeta(**meta)).reason == "risk_understated"


@pytest.mark.parametrize(
    "target",
    [
        "notes/seite.html",
        "notes/seite.HTM",
        "notes/brief.xml",
        "notes/bild.svg",
        "notes/doc.pdf",
        "notes/brief.docx",
        "notes/brief.docm",
        "notes/tabelle.xlsx",
        "notes/folien.pptx",
        "notes/brief.odt",
        "notes/foto.png",
        "notes/lied.mp3",
        "notes/film.mp4",
    ],
)
async def test_documents_pages_and_media_are_not_written_on_a_device(
    fs_world: dict[str, Path], target: str
) -> None:
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path=target, text="<p>x</p>")
        )
    assert caught.value.reason == "format_not_writable"
    assert not (fs_world["root"] / target).exists()


@pytest.mark.parametrize(
    ("target", "text", "reason"),
    [
        ("notes/daten.json", "{kaputt", "invalid_content"),
        ("notes/daten.yml", "a: [unclosed", "invalid_content"),
        ("notes/daten.yml", "!!python/object/apply:os.system ['id']", "content_refused"),
        ("notes/daten.yaml", "a: !eigen x", "content_refused"),
        ("notes/daten.yml", "a: &x [1, 2]\nb: *x\n", "content_refused"),
        ("notes/daten.yaml", "a: &anker 1\n", "content_refused"),
    ],
)
async def test_structured_text_must_parse_plainly(
    fs_world: dict[str, Path], target: str, text: str, reason: str
) -> None:
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text=text))
    assert caught.value.reason == reason
    assert not (fs_world["root"] / target).exists()


async def test_structured_files_are_written_whole_never_appended(fs_world: dict[str, Path]) -> None:
    tools = hand_tools_for(fs_world)
    for target in ("notes/d.json", "notes/d.yml", "notes/d.yaml"):
        with pytest.raises(NodeRefusal) as caught:
            await tools["file.write"].handler(WriteArgs(path=target, text="x", append=True))
        assert caught.value.reason == "append_refused"


@pytest.mark.parametrize(
    ("target", "text"),
    [
        ("notes/daten.json", '{"einkauf": ["Tomaten", "Kaffee"]}'),
        ("notes/liste.yml", "- Haferflocken\n- Tomaten\n"),
        ("notes/plan.yaml", "montag: Mathe Seite 42\n"),
    ],
)
async def test_plain_structured_files_are_written(
    fs_world: dict[str, Path], target: str, text: str
) -> None:
    answer = await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text=text))
    assert answer["ok"] is True


@pytest.mark.parametrize("marker", [".git", ".hg", ".svn"])
async def test_nothing_in_a_working_tree_is_written(fs_world: dict[str, Path], marker: str) -> None:
    project = fs_world["root"] / "projekt"
    (project / "docs").mkdir(parents=True)
    if marker == ".hg":
        (project / marker).write_text("", encoding="utf-8")  # a file marks it too
    else:
        (project / marker).mkdir()
    for target in ("projekt/notiz.txt", "projekt/docs/notiz.txt"):
        with pytest.raises(NodeRefusal) as caught:
            await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text="x"))
        assert caught.value.reason == "working_tree_refused"
        assert not (fs_world["root"] / target).exists()
    ok = await hand_tools_for(fs_world)["file.write"].handler(
        WriteArgs(path="notes/neben.txt", text="x")
    )
    assert ok["ok"] is True  # outside the working tree


async def test_a_link_into_a_working_tree_is_refused(fs_world: dict[str, Path]) -> None:
    project = fs_world["root"].parent / "projekt"
    (project / ".git").mkdir(parents=True)
    link_folder(project, fs_world["root"] / "door")
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(
            WriteArgs(path="door/notiz.txt", text="x")
        )
    assert caught.value.reason in ("working_tree_refused", "link_refused", "outside_folders")
    assert not (project / "notiz.txt").exists()


async def test_an_allowed_folder_that_is_a_working_tree_takes_no_writes(
    fs_world: dict[str, Path],
) -> None:
    (fs_world["root"] / ".git").mkdir()
    with pytest.raises(NodeRefusal) as caught:
        await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path="notiz.txt", text="x"))
    assert caught.value.reason == "working_tree_refused"


@pytest.mark.parametrize("text", ["notes/x.txt ", " notes/x.txt", "notes /x.txt", "notes/ x.txt"])
def test_spaces_around_a_path_or_a_name_are_refused_not_cut(
    fs_world: dict[str, Path], text: str
) -> None:
    assert read_refusal(folders(fs_world), text) == "invalid_path"


async def test_a_refused_name_is_never_created_even_for_a_moment(
    fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node.device.safe_fs import opening

    def never(*_a: Any, **_k: Any) -> int:
        raise AssertionError("the file system was touched for a refused name")

    monkeypatch.setattr(opening, "_posix_open", never)
    monkeypatch.setattr(opening, "_win_open", never)
    for target in ("notes/run.ps1", "notes/.hidden.txt", "notes/settings.json", "notes/tool"):
        with pytest.raises(NodeRefusal):
            await hand_tools_for(fs_world)["file.write"].handler(WriteArgs(path=target, text="x"))
