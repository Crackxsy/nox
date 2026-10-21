"""The node on a desktop: tray state and stop, its own notification ID, and the update loop
inside `nox node run` (confirming a fresh version, restarting into a new one). No Qt, no key hook,
no network: the pieces are tested without a desktop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nox.node import appid, tools
from nox.node.cli import NodeRuntime, keep_updated
from nox.node.home import NodeHome
from nox.node.tray import STOP_KEYS, allowed_here, stop_here, view
from nox.node.update import UpdateCheck
from nox.shell.hotkeys import HotkeyTracker
from nox.shell.logic import HotkeyAction


def test_the_tray_shows_the_nodes_state(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "node")
    assert view(home).tooltip == "Nicht verbunden mit Nox"
    home.write_status({"state": "connected"})
    assert view(home).tint == "normal"
    assert view(home, german=False).tooltip == "Nox: connected"
    home.write_status({"state": "something new"})
    assert view(home).tint == "offline"  # unknown counts as not connected, never as fine


def test_stop_here_is_the_local_stop_and_only_resumes_locally(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "node")
    home.write_status({"state": "connected"})
    stop_here(home)
    assert home.stop_file.exists()
    shown = view(home)
    assert shown.stopped_here and shown.tint == "safe_mode"


def test_the_stop_key_is_ctrl_alt_shift_k() -> None:
    tracker = HotkeyTracker({STOP_KEYS: HotkeyAction.KILL})
    fired: list[tuple[HotkeyAction, bool]] = []
    for key in ("ctrl_l", "alt_l", "shift", "k"):
        fired += tracker.press(key)
    assert fired == [(HotkeyAction.KILL, True)]


def test_what_nox_may_do_here_names_the_allowed_groups(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "node")
    text = allowed_here(home, german=False)
    assert text.startswith("Nox may here:") and "nox node allow" in text


def test_the_toast_names_its_sender_through_the_environment_only() -> None:
    script = tools._WINDOWS_TOAST  # noqa: SLF001
    assert "$env:NOX_NOTIFY_APPID" in script
    assert "1AC14E77" not in script  # no fixed sender in the script any more
    if not __import__("sys").platform == "win32":
        assert appid.toast_app_id() == appid.NODE_APP_ID


class _Agent:
    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.busy = False


class _Updater:
    def __init__(self, pending: bool) -> None:
        self.confirmed = 0
        self.pending = pending

    def confirm_running(self) -> bool:
        self.confirmed += 1
        return True

    def restart_needed(self) -> bool:
        return self.pending

    def check(self) -> UpdateCheck:  # pragma: no cover - the first check is minutes away
        return UpdateCheck("up_to_date", "")


async def _loop(
    tmp_path: Path, updater: _Updater, agent: _Agent, *, rounds: float = 0.2
) -> tuple[asyncio.Event, asyncio.Event]:
    stop, restart = asyncio.Event(), asyncio.Event()
    runtime = NodeRuntime(NodeHome(tmp_path), None, None, None)  # type: ignore[arg-type]
    task = asyncio.create_task(
        keep_updated(runtime, agent, updater, stop, restart, poll_s=0.01)  # type: ignore[arg-type]
    )
    await asyncio.sleep(rounds)
    stop.set()
    await asyncio.wait_for(task, 2)
    return stop, restart


async def test_a_fresh_version_confirms_itself_once_it_is_connected(tmp_path: Path) -> None:
    agent, updater = _Agent(), _Updater(pending=False)
    agent.connected.set()
    await _loop(tmp_path, updater, agent)
    assert updater.confirmed == 1


async def test_under_the_launcher_a_pending_version_restarts_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_NODE_LAUNCHER", "1")
    agent, updater = _Agent(), _Updater(pending=True)
    agent.connected.set()
    _stop, restart = await _loop(tmp_path, updater, agent)
    assert restart.is_set()


async def test_no_restart_while_a_call_runs_or_without_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_NODE_LAUNCHER", "1")
    agent, updater = _Agent(), _Updater(pending=True)
    agent.connected.set()
    agent.busy = True
    assert not (await _loop(tmp_path, updater, agent))[1].is_set()
    monkeypatch.delenv("NOX_NODE_LAUNCHER")
    agent.busy = False
    assert not (await _loop(tmp_path, updater, agent))[1].is_set()
