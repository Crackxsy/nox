"""The dialog and the notice on the device on every platform: macOS through `osascript`
with the refusal as default and cancel button, zenity first and `kdialog` on KDE, a
screen read when asked (never frozen at start), a notice that always says Nox, and no
trust in the core on a device that has a screen. No dialog is ever shown here: the
programs' answers are given."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.device import confirm
from nox.node.device.confirm import MAC_DIALOG, PlatformConfirmer, answer_of, kdialog_text
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import NodeManifest
from tests.unit.node.support import Confirmer, call, session_of, start_node

# ---- the Linux dialog is zenity only, default "No", no markup ------------------------------------


async def test_zenity_first_kdialog_on_kde_and_always_no_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kdialog is the KDE fallback - its `--warningyesno` has "No" as
    the default button; zenity, where it is there, is still preferred."""
    from nox.node.device import confirm as confirm_module

    monkeypatch.setattr(confirm_module.sys, "platform", "linux")
    monkeypatch.setattr(confirm_module, "has_screen", lambda: True)
    monkeypatch.setattr(
        confirm_module.shutil,
        "which",
        lambda name: "/usr/bin/kdialog" if name == "kdialog" else None,
    )
    assert confirm_module.PlatformConfirmer().available is True  # KDE without zenity
    monkeypatch.setattr(confirm_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[list[str]] = []

    class Proc:
        returncode = 1

        async def wait(self) -> int:
            return 1

    async def fake_exec(*argv: str, **_kw: Any) -> Proc:
        seen.append(list(argv))
        return Proc()

    monkeypatch.setattr(confirm_module.asyncio, "create_subprocess_exec", fake_exec)
    assert await confirm_module.PlatformConfirmer().ask("<b>x</b>") == "deny"
    assert seen[0][0].endswith("zenity") and "--default-cancel" in seen[0]
    assert "--no-markup" in seen[0]
    monkeypatch.setattr(confirm_module.shutil, "which", lambda _name: None)
    assert confirm_module.PlatformConfirmer().available is False  # nothing to ask with


async def test_a_notice_always_says_nox_and_uses_the_systems_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.node.device import tools as node_tools

    shown: list[tuple[str, str]] = []

    class Fake:
        async def show(self, title: str, text: str) -> None:
            shown.append((title, text))

    notify = node_tools.default_tools(Fake())["notify.show"]
    await notify.handler(node_tools.NotifyArgs(title="Windows Update", text="x"))
    await notify.handler(node_tools.NotifyArgs(text="y"))
    assert shown == [("Nox: Windows Update", "x"), ("Nox", "y")]

    seen: list[list[str]] = []

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*argv: str, **_kw: Any) -> Proc:
        seen.append(list(argv))
        return Proc()

    monkeypatch.setattr(node_tools.sys, "platform", "win32")
    monkeypatch.setattr(node_tools.asyncio, "create_subprocess_exec", fake_exec)
    await node_tools.PlatformNotifier().show("Nox", "z")
    from pathlib import PureWindowsPath

    program = PureWindowsPath(seen[0][0])  # a Windows path, whatever the test runs on
    assert program.is_absolute() and program.name.lower() == "powershell.exe"
    assert "system32" in str(program).lower()


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
    """`trust_core_for_high` is for a device without a screen. A desktop whose dialog
    program is missing refuses, whatever the manifest says."""
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
