"""The node's own terminal during a computer-use session: commands that widen what the
device allows refuse while a session runs or its marker cannot be read, an update is not
installed, and computer use never clicks into a terminal that runs `nox node`."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.node.support_sessions import plain_node_home


def _session(home: Any, *, until: float | None = None, raw: str | None = None) -> None:
    from nox.node.world import SESSION_MARKER

    marker = home.path / SESSION_MARKER
    if raw is not None:
        marker.write_text(raw, encoding="utf-8")
        return
    marker.write_text(json.dumps({"until": until or time.time() + 600}), encoding="utf-8")


@pytest.mark.parametrize("raw", [None, "not json", '{"until": "soon"}', "[]"])
def test_widening_commands_refuse_while_a_session_runs_or_cannot_be_read(
    tmp_path: Path, raw: str | None
) -> None:
    from nox.node import cli, satellite_cli
    from nox.node.voice import MUTE_FILE

    home = plain_node_home(tmp_path)
    _session(home, raw=raw)
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    with pytest.raises(cli.NodeCliError, match="mouse and keyboard"):
        # `notify` works on every OS: the session refusal is what is tested, not the platform.
        cli.allow(home, "notify", True, say=print, interactive=lambda: True, confirm=confirm)
    with pytest.raises(cli.NodeCliError):
        cli.games(home, "Game.exe", False, say=print, interactive=lambda: True, confirm=confirm)
    with pytest.raises(cli.NodeCliError):
        cli.resume(home)
    (home.path / MUTE_FILE).write_text("muted", encoding="utf-8")
    with pytest.raises(satellite_cli.SatelliteCliError):
        satellite_cli.voice_mute(home, False)
    assert (home.path / MUTE_FILE).exists()
    with pytest.raises(satellite_cli.SatelliteCliError):
        satellite_cli.obs_resume(home)
    assert asked == []  # refused before anything is asked
    # Narrowing always works.
    assert "Microphone muted" in satellite_cli.voice_mute(home, True)
    cli.allow(home, "media", False, say=print)


def test_shell_is_not_available_on_a_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allow shell` on macOS says so plainly, whatever else is going on."""
    from nox.node import cli, cli_text

    real = cli_text.unsupported_reason
    monkeypatch.setattr(cli, "unsupported_reason", lambda group: real(group, "darwin"))
    home = plain_node_home(tmp_path)
    with pytest.raises(cli.NodeCliError, match="not available on this device"):
        cli.allow(home, "shell", True, say=print, interactive=lambda: True, confirm=lambda _p: True)


def test_update_refuses_to_install_during_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import cli

    home = plain_node_home(tmp_path)
    _session(home)
    check = SimpleNamespace(status="available", message="Nox 0.9.0 is available", version="0.9.0")
    applied: list[Any] = []
    updater = SimpleNamespace(
        check=lambda retry_bad: check, stale_days=lambda: None, apply=applied.append
    )
    monkeypatch.setattr(cli, "node_updater", lambda _home: updater)
    said: list[str] = []
    assert cli.update(home, check_only=False, yes=True, say=said.append, ask=lambda _q: True) == 1
    assert applied == [] and "mouse and keyboard" in said[-1]


def test_a_marker_whose_time_is_over_refuses_nothing(tmp_path: Path) -> None:
    from nox.node import cli
    from nox.node.world import clear_session_marker, session_marker_state

    home = plain_node_home(tmp_path)
    _session(home, until=time.time() - 1)
    assert session_marker_state(home.path) is None and cli.session_refusal(home) is None
    _session(home)
    assert session_marker_state(home.path) == "running"
    clear_session_marker(home.path)
    assert session_marker_state(home.path) is None


def test_the_world_keeps_the_marker_while_a_session_runs(tmp_path: Path) -> None:
    from nox.node.world import SESSION_MARKER, World, session_marker_state

    async def notice(_text: str) -> None:
        return None

    world = World(
        notice=notice,
        computer_enabled=lambda: True,
        dialog_open=lambda: False,
        browser_dir=tmp_path / "browser",
        marker=tmp_path / SESSION_MARKER,
    )
    world.session_changed(True, minutes=5)
    assert session_marker_state(tmp_path) == "running"
    world.session_changed(False)
    assert session_marker_state(tmp_path) is None


class _Proc:
    def __init__(self, pid: int, name: str, cmdline: list[str], children: list[Any] | None = None):
        self.pid, self._name, self._cmdline = pid, name, cmdline
        self._children = children or []
        self._parent: Any = None

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def children(self, recursive: bool = False) -> list[Any]:
        if not recursive:
            return list(self._children)
        found: list[Any] = []
        for child in self._children:
            found += [child, *child.children(recursive=True)]
        return found

    def parent(self) -> Any:
        return self._parent


def test_a_terminal_running_nox_node_counts_as_a_nox_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.desktop.computer_use import terminal_runs_nox

    node_cli = _Proc(30, "python.exe", ["python.exe", "-I", "start_node.pyw", "--cli", "node"])
    shell = _Proc(20, "pwsh.exe", ["pwsh.exe"], [node_cli])
    terminal = _Proc(10, "WindowsTerminal.exe", ["WindowsTerminal.exe"], [shell])
    editor = _Proc(40, "notepad.exe", ["notepad.exe"])
    other_shell = _Proc(50, "cmd.exe", ["cmd.exe"], [_Proc(51, "ping.exe", ["ping"])])
    secret = _Proc(60, "conhost.exe", ["conhost.exe"])
    procs = {p.pid: p for p in (node_cli, shell, terminal, editor, other_shell, secret)}

    def process(pid: int) -> Any:
        if pid == 60:
            return _Unreadable(pid)
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    class _Unreadable(_Proc):
        def __init__(self, pid: int) -> None:
            super().__init__(pid, "conhost.exe", [])

        def children(self, recursive: bool = False) -> list[Any]:
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil, "Process", process)

    def never(_pid: int) -> bool:
        return False

    assert terminal_runs_nox(10, is_nox=never)  # Windows Terminal with nox-node beneath it
    assert terminal_runs_nox(20, is_nox=never)
    assert not terminal_runs_nox(40, is_nox=never)  # no terminal
    assert not terminal_runs_nox(50, is_nox=never)  # a terminal without Nox
    assert terminal_runs_nox(60, is_nox=never)  # a tree that cannot be read: fail closed
    assert not terminal_runs_nox(99, is_nox=never)  # gone


def test_computer_use_never_clicks_into_a_terminal_running_nox() -> None:
    from nox.desktop.computer_use import ComputerUse

    use = ComputerUse(
        enabled=lambda: True,
        game_running=lambda: False,
        game_in_foreground=lambda: False,
        nox_pids=set,
        sender=lambda: None,  # type: ignore[arg-type,return-value]
        audit=lambda *_a: None,
        nox_process=lambda _pid: False,
        terminal_nox=lambda pid: pid == 10,
    )
    assert use._is_nox((10, "Windows PowerShell"))
    assert not use._is_nox((11, "Windows PowerShell"))
    broken = ComputerUse(
        enabled=lambda: True,
        game_running=lambda: False,
        game_in_foreground=lambda: False,
        nox_pids=set,
        sender=lambda: None,  # type: ignore[arg-type,return-value]
        audit=lambda *_a: None,
        nox_process=lambda _pid: False,
        terminal_nox=lambda _pid: 1 / 0 > 0,
    )
    assert broken._is_nox((12, "x"))  # cannot tell: Nox's
