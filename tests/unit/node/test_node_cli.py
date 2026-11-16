"""The words of `nox node` in the terminal: one question to allow a folder, what exists on
which device, yes and no in both languages, and pairing again only when the pairing
cannot work."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.node.support_sessions import plain_node_home

# ---- UX: the terminal's words --------------------------------------------------------------------


def test_allowing_a_folder_switches_files_on_in_one_question(tmp_path: Path) -> None:
    from nox.node import cli
    from nox.node.manifest import load_manifest

    home = plain_node_home(tmp_path)
    cli.confirm_defaults(home, None)
    folder = tmp_path / "Ablage"
    folder.mkdir()
    asked: list[str] = []
    said: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    cli.allow(
        home,
        "files",
        True,
        say=said.append,
        interactive=lambda: True,
        confirm=confirm,
        folder=str(folder),
    )  # noqa: E501
    manifest = load_manifest(home.manifest_file)
    assert manifest.groups["files"] and manifest.folders == [str(folder.resolve())]
    assert asked == [f"Nox may then read and write files in {folder.resolve()}. Allow?"]


def test_allowing_files_without_a_folder_says_so(tmp_path: Path) -> None:
    from nox.node import cli

    home = plain_node_home(tmp_path)
    cli.confirm_defaults(home, None)
    said: list[str] = []
    cli.allow(
        home, "files", True, say=said.append, interactive=lambda: True, confirm=lambda _p: True
    )
    assert "no folder allowed yet" in said[-1] and "allow files --folder" in said[-1]


@pytest.mark.parametrize(
    ("group", "platform", "reason"),
    [
        ("packages", "win32", "not_yet"),
        ("desktop", "linux", "not_here"),
        ("computer", "darwin", "not_here"),
        ("shell", "darwin", "not_here"),
        ("media", "darwin", "not_here"),
        ("media", "linux", None),
        ("files", "darwin", None),
    ],
)
def test_what_exists_on_which_device(group: str, platform: str, reason: str | None) -> None:
    from nox.node.cli_text import unsupported_reason

    assert unsupported_reason(group, platform) == reason


def test_allow_refuses_a_group_that_cannot_work_here(tmp_path: Path) -> None:
    from nox.node import cli

    home = plain_node_home(tmp_path)
    with pytest.raises(cli.NodeCliError, match="not available on devices yet"):
        cli.allow(
            home, "packages", True, say=print, interactive=lambda: True, confirm=lambda _p: True
        )


def test_yes_and_no_in_both_languages() -> None:
    from nox.node.cli_text import ask_yes

    prompts: list[str] = []

    def reader(answer: str) -> Any:
        def read(prompt: str) -> str:
            prompts.append(prompt)
            return answer

        return read

    assert ask_yes("Erlauben?", lang="de", read=reader("j"))
    assert ask_yes("Allow?", lang="en", read=reader("ja"))
    assert ask_yes("Allow?", lang="en", read=reader("Y"))
    assert not ask_yes("Erlauben?", lang="de", read=reader(""))
    assert not ask_yes("Erlauben?", lang="de", read=reader("n"))
    assert prompts[0].endswith("[j/N] ") and prompts[1].endswith("[y/N] ")


def test_the_command_is_named_as_the_device_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node import cli_text

    monkeypatch.delenv("NOX_NODE_HOME", raising=False)
    monkeypatch.setattr(cli_text.getpass, "getuser", lambda: "alex")
    assert cli_text.node_command("win32") == "nox-node"
    assert cli_text.node_command("linux") == "nox-node"
    monkeypatch.setenv("NOX_NODE_HOME", "/var/lib/nox-node/node")
    assert cli_text.node_command("linux") == "sudo nox-node"
    assert cli_text.node_command("win32") == "nox-node"


def test_german_words_and_labels_match_the_devices_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node.cli_text import group_label, language, t

    monkeypatch.setenv("NOX_LANG", "de")
    assert language() == "de"
    assert group_label("files") == "Dateien lesen und schreiben"
    assert group_label("camera") == "Kamera nutzen"
    assert group_label("voice") == "Zuhören und sprechen"
    assert t("allow_ask_folder", folder="D:/Ablage") == (
        "Nox darf dann Dateien in D:/Ablage lesen und schreiben. Erlauben?"
    )
    assert t("allow_files_no_folder", add="x").startswith("Dateien: an, aber noch kein Ordner")
    monkeypatch.setenv("NOX_LANG", "en")
    assert group_label("files") == "read and write files"


# ---- N1: pairing again ---------------------------------------------------------------------------


def test_pair_again_only_when_the_pairing_cannot_work(tmp_path: Path) -> None:
    from nox.node import cli

    home = plain_node_home(tmp_path)
    link = "nox-node://pair?h=192.168.1.20&p=47843&c=ABCDEFGH&fp=" + "ab" * 32
    with pytest.raises(cli.NodeCliError, match="not paired"):
        cli.pair(link, home=home, again=True)
    identity = SimpleNamespace()
    home.load_identity = lambda: identity  # type: ignore[method-assign]
    home.pinned_ca_ok = lambda _i: True  # type: ignore[method-assign]
    home.read_status = lambda: {"state": "refused"}  # type: ignore[method-assign]
    assert cli.again_reason(home) == "refused"
    home.read_status = lambda: {"state": "connected"}  # type: ignore[method-assign]
    assert cli.again_reason(home) == "certificate_unreadable"  # no certificate file
    home.pinned_ca_ok = lambda _i: False  # type: ignore[method-assign]
    assert cli.again_reason(home) == "pin_mismatch"


def test_render_node_names_the_real_command() -> None:
    from nox.lan.terminal import render_node

    shown = render_node(
        {
            "link": "nox-node://pair?h=192.168.1.20",
            "code": "ABCDEFGH",
            "expires_at": "2026-10-24T12:00:00+00:00",
            "ca_fingerprint": "AB:" * 10,
        },
        language="en",
    )
    assert 'nox-node pair "nox-node://pair?h=192.168.1.20"' in shown and "sudo nox-node" in shown
    assert "nox node pair" not in shown
