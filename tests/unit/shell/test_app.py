"""ShellApp with a fake bridge: offline behaviour, events -> tray, permission -> reply, kill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nox.core.state import PrivacyMode
from nox.shell.app import ShellApp
from nox.shell.ipc_bridge import BridgeUnavailableError
from nox.shell.logic import HotkeyAction, TrayTint, tray_tint
from nox.shell.supervisor_client import SupervisorUnavailableError

CONFIG: dict[str, Any] = {"identity": {"ui_language": "en"}}


def make_runtime(tmp_path: Path, *, with_core: bool = True, with_sup: bool = True) -> Path:
    if with_core:
        (tmp_path / "session.token").write_text("session-token-1234567890", encoding="utf-8")
        (tmp_path / "ipc.json").write_text(json.dumps({"ws_port": 47800, "http_port": 47801}))
    if with_sup:
        (tmp_path / "supervisor.token").write_text("sup-token", encoding="utf-8")
    return tmp_path


def make_app(tmp_path: Path, factory: Any, **kw: Any) -> ShellApp:
    return ShellApp(
        runtime_dir=tmp_path,
        config=CONFIG,
        bridge_factory=factory,
        enable_hotkeys=False,
        create_pet_window=False,
        open_url=lambda _url: None,
        **kw,
    )


def test_connects_subscribes_and_pings(qapp: Any, tmp_path: Path, fake_bridge_factory: Any) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    bridge = created[0]
    assert bridge.started and bridge.role == "shell" and bridge.url == "ws://127.0.0.1:47800/ws"
    assert bridge.token == "session-token-1234567890"
    names = [n for n, _ in bridge.calls]
    assert names[0] == "ipc.subscribe" and "ipc.ping" in names
    assert app.model.connected is True
    assert app.tray.refresh(app.model) == TrayTint.NORMAL
    app.quit()
    assert bridge.stopped


def test_offline_without_runtime_files(qapp: Any, tmp_path: Path, fake_bridge_factory: Any) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path, with_core=False), factory)
    app.start()
    qapp.processEvents()
    assert created == []
    assert app.model.connected is False
    assert tray_tint(app.model) == TrayTint.OFFLINE
    # actions are dropped, not faked
    app.set_privacy(PrivacyMode.PRIVATE)
    assert app.calls[-1] == ("privacy.set", {"mode": "private"})
    assert app.model.privacy_mode == PrivacyMode.BALANCED
    app.quit()


def test_bridge_unavailable_is_offline(qapp: Any, tmp_path: Path) -> None:
    def factory(*_a: Any) -> Any:
        raise BridgeUnavailableError("no client")

    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    assert app.model.connected is False
    app.quit()


def test_ping_failure_marks_offline(qapp: Any, tmp_path: Path, fake_bridge_factory: Any) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    assert app.model.connected
    created[0].fail_calls = True
    app._ping()
    qapp.processEvents()
    assert app.model.connected is False
    app.quit()


def test_events_update_tray_tint(qapp: Any, tmp_path: Path, fake_bridge_factory: Any) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    created[0].emit(
        "privacy.capture_changed",
        {"microphone": True, "camera": False, "screen": False, "cloud": False},
    )
    qapp.processEvents()
    assert app.model.capture.microphone and tray_tint(app.model) == TrayTint.CAPTURING
    created[0].emit("voice.muted", {"muted": True})
    created[0].emit(
        "privacy.capture_changed",
        {"microphone": False, "camera": False, "screen": False, "cloud": False},
    )
    qapp.processEvents()
    assert tray_tint(app.model) == TrayTint.MUTED
    assert app.tray.action_mute.isChecked()
    app.quit()


def test_permission_dialog_reply_and_session_memory(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    factory, created = fake_bridge_factory
    asked: list[dict[str, Any]] = []

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        asked.append(payload)
        return {"grant_id": payload["request_id"], "decision": "allow", "remember": True}

    app = make_app(make_runtime(tmp_path), factory, permission_handler=handler)
    app.start()
    qapp.processEvents()
    req = {
        "request_id": "r1",
        "agent": "coder",
        "tool": "fs",
        "action": "write",
        "mode": "coding",
        "risk": "medium",
    }
    created[0].emit("security.permission_requested", req)
    created[0].emit("security.permission_requested", {**req, "request_id": "r2"})
    qapp.processEvents()
    assert len(asked) == 1  # second one answered from session memory
    replies = [p for n, p in created[0].calls if n == "security.permission.reply"]
    assert replies == [
        {"grant_id": "r1", "decision": "allow", "remember": True},
        {"grant_id": "r2", "decision": "allow", "remember": True},
    ]
    app.quit()


def test_kill_switch_goes_to_core_when_connected(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    assert app.kill_switch("tray") == "core"
    assert ("security.kill", {"reason": "user", "origin": "tray"}) in created[0].calls
    app.quit()


def test_kill_switch_falls_back_to_supervisor(qapp: Any, tmp_path: Path) -> None:
    sent: list[tuple[str, int, str]] = []

    def sup_kill(host: str, port: int, token: str) -> dict[str, Any]:
        sent.append((host, port, token))
        return {"ok": True}

    def factory(*_a: Any) -> Any:
        raise BridgeUnavailableError("no client")

    app = make_app(make_runtime(tmp_path), factory, supervisor_kill=sup_kill)
    app.start()
    assert app.kill_switch("hotkey") == "supervisor"
    assert sent == [("127.0.0.1", 47799, "sup-token")]
    app.quit()


def test_kill_switch_reports_failure_honestly(qapp: Any, tmp_path: Path) -> None:
    def factory(*_a: Any) -> Any:
        raise BridgeUnavailableError("no client")

    def sup_kill(host: str, port: int, token: str) -> dict[str, Any]:
        raise SupervisorUnavailableError("down")

    app = make_app(make_runtime(tmp_path), factory, supervisor_kill=sup_kill)
    app.start()
    assert app.kill_switch("tray") == "failed"
    app.quit()

    nosup = tmp_path / "nosup"
    nosup.mkdir()
    app2 = make_app(make_runtime(nosup, with_sup=False), factory, supervisor_kill=sup_kill)
    app2.start()
    assert app2.kill_switch("tray") == "failed"  # no supervisor token: reported, never faked
    app2.quit()


def test_quit_sends_sup_stop_to_supervisor(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    """B-6: Quit always asks the supervisor to stop core, shell and itself - never a bare exit."""
    factory, _created = fake_bridge_factory
    sent: list[tuple[str, int, str]] = []

    def sup_stop(host: str, port: int, token: str) -> dict[str, Any]:
        sent.append((host, port, token))
        return {"ok": True}

    app = make_app(make_runtime(tmp_path), factory, supervisor_stop=sup_stop)
    app.start()
    qapp.processEvents()
    app.quit()
    assert sent == [("127.0.0.1", 47799, "sup-token")]


def test_quit_without_supervisor_token_does_not_crash(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    factory, _created = fake_bridge_factory
    sent: list[tuple[str, int, str]] = []

    def sup_stop(host: str, port: int, token: str) -> dict[str, Any]:
        sent.append((host, port, token))
        return {"ok": True}

    app = make_app(make_runtime(tmp_path, with_sup=False), factory, supervisor_stop=sup_stop)
    app.start()
    app.quit()  # no supervisor.token on disk: reported, never faked, never raises
    assert sent == []


def test_hotkeys_map_to_requests(qapp: Any, tmp_path: Path, fake_bridge_factory: Any) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    app.apply_hotkey(HotkeyAction.PTT, True)
    app.apply_hotkey(HotkeyAction.PTT, False)
    app.apply_hotkey(HotkeyAction.MUTE, True)
    app.apply_hotkey(HotkeyAction.MUTE, False)  # release ignored
    app.apply_hotkey(HotkeyAction.PRIVACY, True)
    app.apply_hotkey(HotkeyAction.TOGGLE_PET, True)
    reqs = [c for c in created[0].calls if c[0] not in ("ipc.subscribe", "ipc.ping")]
    assert reqs == [
        ("voice.ptt", {"pressed": True}),
        ("voice.ptt", {"pressed": False}),
        ("voice.mute", {"muted": True}),
        ("privacy.set", {"mode": "private"}),
    ]
    assert app.model.pet_visible is False
    assert json.loads((tmp_path / "shell.json").read_text())["visible"] is False
    app.quit()


@pytest.mark.parametrize("lang", ["de", "en"])
def test_tray_language(qapp: Any, tmp_path: Path, fake_bridge_factory: Any, lang: str) -> None:
    factory, _ = fake_bridge_factory
    app = ShellApp(
        runtime_dir=make_runtime(tmp_path),
        config={"identity": {"ui_language": lang}},
        bridge_factory=factory,
        enable_hotkeys=False,
        create_pet_window=False,
    )
    expected = "Beenden" if lang == "de" else "Quit"
    assert app.tray.action_quit.text() == expected
