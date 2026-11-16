"""The dialog on the device on every platform (review panel round 1, M1/M2): macOS through
`osascript` with "Ablehnen" as default and cancel button, KDE through `kdialog` when zenity is
missing, a screen read when asked (never frozen at start), and no trust in the core on a device
that has a screen. No dialog is ever shown here: the programs' answers are given."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node import confirm
from nox.node.confirm import MAC_DIALOG, PlatformConfirmer, answer_of, kdialog_text
from nox.node.listener import NodeListener
from nox.node.manifest import NodeManifest
from nox.node.pairing import NodePairing
from tests.unit.node.test_c4 import TARGET, Confirmer, call, session_of, start_node


@pytest.mark.parametrize(
    ("program", "code", "out", "answer"),
    [
        ("/usr/bin/osascript", 0, b"allow\n", "allow"),
        ("/usr/bin/osascript", 0, b"timeout\n", "timeout"),
        ("/usr/bin/osascript", 0, b"deny\n", "deny"),
        ("/usr/bin/osascript", 1, b"", "deny"),  # Escape: error -128
        ("/usr/bin/osascript", 0, b"allow please", "deny"),  # never a guess
        ("/usr/bin/zenity", 0, None, "allow"),
        ("/usr/bin/zenity", 5, None, "timeout"),
        ("/usr/bin/zenity", 1, None, "deny"),
        ("/usr/bin/kdialog", 0, None, "allow"),
        ("/usr/bin/kdialog", 1, None, "deny"),
        ("/usr/bin/kdialog", 2, None, "deny"),
        ("powershell.exe", 2, None, "timeout"),
        ("powershell.exe", 1, None, "deny"),
    ],
)
def test_only_an_unmistakable_yes_is_a_yes(
    program: str, code: int, out: bytes | None, answer: str
) -> None:
    assert answer_of(program, code, out) == answer


def test_the_mac_dialog_defaults_to_no_and_gets_the_text_as_data() -> None:
    confirmer = PlatformConfirmer(platform="darwin")
    text = 'Nox möchte "x" & do shell script "rm -rf ~"'
    argv = confirmer.argv(text, 60)
    assert argv is not None and argv[:3] == ["/usr/bin/osascript", "-e", MAC_DIALOG]
    assert argv[3] == text and argv[4] == "60"  # data, never script text
    assert 'default button "Ablehnen"' in MAC_DIALOG and 'cancel button "Ablehnen"' in MAC_DIALOG
    assert "giving up after" in MAC_DIALOG and text not in MAC_DIALOG


def test_on_kde_without_zenity_kdialog_asks_with_no_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirm, "has_screen", lambda: True)
    monkeypatch.setattr(
        confirm.shutil, "which", lambda name: "/usr/bin/kdialog" if name == "kdialog" else None
    )
    confirmer = PlatformConfirmer(platform="linux")
    assert confirmer.available
    argv = confirmer.argv("Datei <b>löschen</b>?\nJa", 60)
    assert argv is not None and argv[0] == "/usr/bin/kdialog" and "--warningyesno" in argv
    shown = argv[-1]
    assert shown == "<qt>Datei &lt;b&gt;löschen&lt;/b&gt;?<br>Ja</qt>"  # never formats itself
    assert kdialog_text("a & b") == "<qt>a &amp; b</qt>"
    monkeypatch.setattr(
        confirm.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "zenity" else None
    )
    argv = confirmer.argv("x", 60)
    assert argv is not None and argv[0] == "/usr/bin/zenity" and "--default-cancel" in argv
    monkeypatch.setattr(confirm.shutil, "which", lambda _name: None)
    assert not confirmer.available  # no dialog program: high risk is refused, never trusted


def test_the_screen_is_read_when_asked_and_taken_from_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A desktop node started before the graphical session: no DISPLAY in its own environment
    at first, then the session's display from the user manager - and gone again with it."""
    monkeypatch.setattr(confirm, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(confirm, "_socket_there", lambda _variable, value: value == ":0")
    for name in confirm.SESSION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    session: dict[str, str] = {}
    monkeypatch.setattr(confirm, "_user_manager_environment", lambda: dict(session))
    assert confirm.has_screen() is False
    session.update({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
    assert confirm.has_screen() is True
    assert confirm.os.environ.get("DISPLAY") == ":0"  # taken over for dialogs and notices
    monkeypatch.setenv("DISPLAY", ":7")  # a display whose socket is gone
    session.clear()
    assert confirm.has_screen() is False


def test_the_user_manager_is_asked_only_for_session_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirm._session_cache.clear()
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(confirm.shutil, "which", lambda _name: "/usr/bin/systemctl")
    out = "DISPLAY=:0\nPATH=/evil\nWAYLAND_DISPLAY=wayland-0\nLD_PRELOAD=/x.so\n"
    monkeypatch.setattr(confirm.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=out))
    found = confirm._user_manager_environment()
    confirm._session_cache.clear()
    assert found == {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}


async def test_a_device_with_a_screen_never_trusts_the_core_for_high_risk(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """D1: `trust_core_for_high` is for a device without a screen. A desktop whose dialog
    program is missing refuses, whatever the manifest says."""
    saved = dict(TARGET)  # `start_node` points the shared receipt target at this node
    confirmer: Any = Confirmer(available=False)
    confirmer.screen = True
    node = await start_node(
        listener,
        pairing,
        tls,
        tmp_path,
        confirmer=confirmer,
        manifest=NodeManifest(trust_core_for_high=True),
    )
    try:
        session = await session_of(listener, node.node_id)
        result = await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        assert result["_nox"]["refused"] == "high_needs_local_confirmation" and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)
        TARGET.update(saved)
