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
        self.protocol_refused = False


class _Updater:
    def __init__(self, pending: bool, *, trial: bool = True, fail_once: bool = False) -> None:
        self.confirmed = 0
        self.pending = pending
        self.trial = trial
        self.fail_once = fail_once

    def on_trial(self) -> bool:
        return self.trial

    def confirm_running(self) -> bool:
        if self.fail_once:
            self.fail_once = False
            raise OSError("state.json is locked")
        self.confirmed += 1
        self.trial = False
        return True

    def restart_needed(self) -> bool:
        return self.pending

    def stale_days(self) -> int | None:
        return None

    def check(self) -> UpdateCheck:  # pragma: no cover - the first check is minutes away
        return UpdateCheck("up_to_date", "")


async def _loop(
    tmp_path: Path, updater: _Updater, agent: _Agent, *, rounds: float = 0.3
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    stop, restart, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runtime = NodeRuntime(NodeHome(tmp_path), None, None, None)  # type: ignore[arg-type]
    task = asyncio.create_task(
        keep_updated(
            runtime,
            agent,  # type: ignore[arg-type]
            updater,  # type: ignore[arg-type]
            stop,
            restart,
            poll_s=0.01,
            trial_failed=failed,
        )
    )
    await asyncio.sleep(rounds)
    stop.set()
    await asyncio.wait_for(task, 2)
    return stop, restart, failed


async def test_a_version_on_trial_is_confirmed_only_once_its_core_accepted_it(
    tmp_path: Path,
) -> None:
    agent, updater = _Agent(), _Updater(pending=False)
    await _loop(tmp_path, updater, agent, rounds=0.15)
    assert updater.confirmed == 0  # running is not enough
    agent.connected.set()
    await _loop(tmp_path, updater, agent)
    assert updater.confirmed == 1


async def test_a_core_that_refuses_the_new_protocol_ends_the_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_NODE_LAUNCHER", "1")
    agent, updater = _Agent(), _Updater(pending=False)
    agent.protocol_refused = True
    stop, _restart, failed = await _loop(tmp_path, updater, agent)
    assert failed.is_set() and stop.is_set() and updater.confirmed == 0


async def test_an_error_in_one_round_does_not_end_the_loop_or_mark_anything_bad(
    tmp_path: Path,
) -> None:
    agent, updater = _Agent(), _Updater(pending=False, fail_once=True)
    agent.connected.set()
    _stop, _restart, failed = await _loop(tmp_path, updater, agent, rounds=0.5)
    assert updater.confirmed == 1 and not failed.is_set()


async def test_under_the_launcher_a_pending_version_restarts_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_NODE_LAUNCHER", "1")
    agent, updater = _Agent(), _Updater(pending=True, trial=False)
    _stop, restart, _failed = await _loop(tmp_path, updater, agent)
    assert restart.is_set()


async def test_no_restart_while_a_call_runs_or_without_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_NODE_LAUNCHER", "1")
    agent, updater = _Agent(), _Updater(pending=True, trial=False)
    agent.busy = True
    assert not (await _loop(tmp_path, updater, agent))[1].is_set()
    monkeypatch.delenv("NOX_NODE_LAUNCHER")
    agent.busy = False
    assert not (await _loop(tmp_path, updater, agent))[1].is_set()


def test_the_tray_starts_isolated_in_the_app_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """No module from the current folder, PYTHONPATH or a user site can stand in for Nox's."""
    from nox.node import tray

    seen: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        seen.update(argv=argv, **kwargs)
        return object()

    monkeypatch.setattr(tray, "tray_available", lambda: True)
    monkeypatch.setattr(tray.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PYTHONPATH", "somewhere-else")
    monkeypatch.delenv("NOX_NODE_TRAY", raising=False)
    assert tray.spawn_tray() is not None
    argv = seen["argv"]
    app = str(Path(tray.__file__).resolve().parents[2])
    assert isinstance(argv, list) and argv[1] == "-I"
    assert seen["cwd"] == app
    env = seen["env"]
    assert isinstance(env, dict) and env["NOX_NODE_APP"] == app
    assert not any(k.upper().startswith("PYTHON") for k in env)


def test_the_trial_start_runs_isolated_in_the_new_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import update

    seen: dict[str, object] = {}

    class Done:
        returncode = 0
        stdout = "0.5.1\n"

    def fake_run(argv: list[str], **kwargs: object) -> Done:
        seen.update(argv=argv, **kwargs)
        return Done()

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONHOME", "elsewhere")
    assert update._default_smoke(tmp_path, "python-here") == "0.5.1"  # noqa: SLF001
    # Isolated, and no bytecode written into the folder the launcher checks file by file.
    assert seen["argv"][:3] == ["python-here", "-I", "-B"]  # type: ignore[index]
    assert seen["cwd"] == str(tmp_path)
    assert not any(k.upper().startswith("PYTHON") for k in seen["env"])  # type: ignore[attr-defined]


def test_one_tray_per_node_and_it_ends_with_its_node(tmp_path: Path) -> None:
    import os

    from nox.node.tray import parent_alive, single_instance

    home = NodeHome(tmp_path / "node")
    first = single_instance(home)
    assert first is not None
    assert single_instance(home) is None  # a second tray for the same node does not start
    first.close()
    again = single_instance(home)
    assert again is not None
    again.close()
    assert parent_alive(str(os.getpid())) and parent_alive(None)
    assert not parent_alive("999999999")


class _Core:
    """A core that answers the hello and then stays quiet."""

    def __init__(self, result: dict[str, object]) -> None:
        self._answer: dict[str, object] | None = {"jsonrpc": "2.0", "id": 1, "result": result}

    async def send(self, _data: str) -> None:
        return None

    async def recv(self) -> str:
        import json

        if self._answer is None:
            await asyncio.sleep(3600)
        answer, self._answer = self._answer, None
        return json.dumps(answer)


@pytest.mark.parametrize("compatible", [True, False])
async def test_only_a_core_that_accepts_this_node_counts_as_connected(
    tmp_path: Path, compatible: bool
) -> None:
    """H1 on the node: the protocol range the core accepts is kept for update decisions, and a
    core that refuses this node's protocol never confirms a version on trial."""
    from datetime import UTC, datetime

    from nox.node.agent import NodeAgent
    from nox.node.gate import NodeGate
    from nox.node.home import NodeIdentity
    from nox.node.keystore import NodeKeyStore
    from nox.node.manifest import NodeManifest
    from nox.node.tools import default_tools

    home = NodeHome(tmp_path / "h")
    home.ensure()
    identity = NodeIdentity(
        node_id="a" * 32,
        name="Laptop",
        core_host="127.0.0.1",
        core_port=1,
        node_port=1,
        ca_fingerprint="b" * 64,
        paired_at=datetime.now(UTC),
    )
    home.save_identity(identity)
    result = {
        "node_id": identity.node_id,
        "core_id": identity.ca_fingerprint,
        "compatible": compatible,
        "node_protocol": [2, 3],
    }
    agent = NodeAgent(
        home,
        NodeKeyStore(tmp_path / "k", None),
        NodeGate(default_tools(), NodeManifest(), None),
        None,
        ping_interval_s=3600,
    )
    stop = asyncio.Event()
    session = asyncio.create_task(agent._session(_Core(result), identity, stop))  # type: ignore[arg-type]  # noqa: SLF001
    await asyncio.sleep(0.2)
    assert agent.connected.is_set() is compatible
    assert agent.protocol_refused is not compatible
    saved = home.load_identity()
    assert saved is not None and saved.core_node_protocol == (2, 3)
    stop.set()
    await asyncio.wait_for(session, 5)
