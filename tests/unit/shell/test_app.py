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


def test_high_risk_is_asked_every_time_even_if_remembered(
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
        "agent": "nox.chat",
        "tool": "obs",
        "action": "scene.delete",
        "mode": "stream",
        "risk": "high",
    }
    created[0].emit("security.permission_requested", req)
    created[0].emit("security.permission_requested", {**req, "request_id": "r2"})
    qapp.processEvents()
    assert [p["request_id"] for p in asked] == ["r1", "r2"]
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
    """Quit always asks the supervisor to stop core, shell and itself - never a bare exit."""
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
    reqs = [
        c
        for c in created[0].calls
        if c[0] not in ("ipc.subscribe", "ipc.ping", "node.computer.status")
    ]
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


def test_settings_changed_reloads_the_pet_page_with_the_new_variant(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    """#24: `config.set pet.variant` publishes `settings.changed {paths: ["pet.variant"]}`; the
    shell re-reads the variant and reloads the pet page with it. The pet page does its own live
    swap on the same event; this reload is the fallback that always works."""
    from types import SimpleNamespace

    factory, created = fake_bridge_factory
    config: dict[str, Any] = {"identity": {"ui_language": "en"}, "pet": {"variant": "fox"}}
    app = ShellApp(
        runtime_dir=make_runtime(tmp_path),
        config=config,
        bridge_factory=factory,
        enable_hotkeys=False,
        create_pet_window=False,
        open_url=lambda _url: None,
    )
    loads: list[str] = []
    app.pet = SimpleNamespace(load=loads.append, show_offline_page=lambda: loads.append("offline"))
    app._load_pet_page()
    app._connect_bridge()
    qapp.processEvents()
    assert "settings.changed" in created[0].calls[0][1]["patterns"]
    assert len(loads) == 1 and "variant=fox" in loads[0]

    config["pet"]["variant"] = "sprite:placeholder"
    created[0].emit("settings.changed", {"paths": ["pet.variant"]})
    qapp.processEvents()
    assert len(loads) == 2 and "variant=sprite:placeholder" in loads[1]

    # Unrelated settings, and a malformed payload, do not touch the window.
    created[0].emit("settings.changed", {"paths": ["identity.ui_language"]})
    created[0].emit("settings.changed", {})
    created[0].emit("settings.changed", {"paths": "pet.variant"})
    qapp.processEvents()
    assert len(loads) == 2
    app._ping_timer.stop()
    app._reconnect_timer.stop()


# ---- the pet around games --------------------------------------------------------------------


class _Pet:
    """What `ShellApp` asks of the pet window around games."""

    def __init__(self) -> None:
        self.visible: bool | None = None
        self.frames: list[dict[str, Any]] = []
        self.monitor = ""

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt API
        self.visible = visible

    def set_frames(self, message: dict[str, Any]) -> None:
        self.frames.append(message)

    def set_default_monitor(self, setting: str) -> None:
        self.monitor = setting

    def x(self) -> int:
        return 10

    def y(self) -> int:
        return 20


def _quiet_app(tmp_path: Path, factory: Any, pet_config: dict[str, Any] | None = None) -> Any:
    config: dict[str, Any] = {"identity": {"ui_language": "en"}, "pet": pet_config or {}}
    app = ShellApp(
        runtime_dir=make_runtime(tmp_path),
        config=config,
        bridge_factory=factory,
        enable_hotkeys=False,
        create_pet_window=False,
        open_url=lambda _url: None,
    )
    app.pet = _Pet()
    return app


def test_the_pet_hides_over_a_full_screen_game_and_comes_back(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.apply_observation(Observed(game_running=True, fullscreen=True))
    assert app.pet.visible is False
    assert app.pet.frames[-1]["gameActive"] is True
    app.apply_observation(Observed())
    assert app.pet.visible is True
    assert app.pet.frames[-1]["gameActive"] is False


def test_a_windowed_game_slows_the_pet_but_does_not_hide_it(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.apply_observation(Observed(game_running=True))
    assert app.pet.visible is None, "nothing to hide or show"
    assert app.pet.frames[-1] == {"fpsTarget": 60, "fpsInGame": 20, "gameActive": True}


def test_the_pet_stays_when_hiding_in_full_screen_is_switched_off(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0], {"hide_in_fullscreen": False})
    app.apply_observation(Observed(fullscreen=True))
    assert app.pet.visible is None
    assert app.pet.frames[-1]["gameActive"] is True


def test_a_pet_the_user_hid_stays_hidden_when_the_game_ends(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.apply_observation(Observed(fullscreen=True))
    app.toggle_pet()  # the user hides it by hand while it is away
    assert app.model.pet_visible is False
    app.apply_observation(Observed())
    assert app.pet.visible is False
    app.toggle_pet()
    assert app.pet.visible is True


def test_toggling_the_pet_during_a_full_screen_game_does_not_bring_it_back(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.apply_observation(Observed(fullscreen=True))
    app.toggle_pet()  # hide
    app.toggle_pet()  # show again - but a game owns the screen
    assert app.model.pet_visible is True
    assert app.pet.visible is False


def test_what_the_user_chose_is_saved_not_what_the_game_did(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.apply_observation(Observed(fullscreen=True))
    app.toggle_pet()
    app.toggle_pet()
    assert app.state.visible is True


def test_the_pet_setting_paths_are_applied_when_they_change(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    from nox.shell.pet_quiet import Observed

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app.config["pet"] = {"default_monitor": "right", "hide_in_fullscreen": False}
    app._apply_settings_changed({"paths": ["pet.default_monitor", "pet.hide_in_fullscreen"]})
    assert app.pet.monitor == "right"
    app.apply_observation(Observed(fullscreen=True))
    assert app.pet.visible is None


def test_a_look_at_the_machine_runs_off_the_ui_thread_and_is_applied_on_it(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    import threading
    import time

    from nox.shell.pet_quiet import Observed

    seen: dict[str, Any] = {}

    def observer(watched: Any) -> Observed:
        seen["thread"] = threading.current_thread()
        seen["watched"] = tuple(watched)
        return Observed(fullscreen=True)

    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app._observer = observer
    app._poll_quiet()
    app._poll_quiet()  # a look still under way is not started twice
    deadline = time.monotonic() + 10
    while app.pet.visible is None and time.monotonic() < deadline:
        qapp.processEvents()
    assert app.pet.visible is False
    assert seen["thread"] is not threading.main_thread()
    assert seen["watched"] == ("RocketLeague.exe",)
    assert app._observing is False
    app._observer_pool.shutdown(wait=True)


def test_an_observer_that_fails_leaves_the_pet_as_it_is(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app._observing = True
    app._on_observed(None)
    assert app._observing is False
    assert app.pet.visible is None


def test_a_settings_event_with_odd_paths_is_ignored(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    app = _quiet_app(tmp_path, fake_bridge_factory[0])
    app._apply_settings_changed({"paths": [["pet.default_monitor"], 3, None]})
    app._apply_settings_changed({"paths": "pet.default_monitor"})
    assert app.pet.monitor == ""


def test_the_configured_default_monitor_reaches_the_pet_window(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    """`pet.default_monitor` and the frame budget are handed to the window the shell creates."""
    built: list[dict[str, Any]] = []

    class FakeWindow(_Pet):
        def __init__(self, state: Any, **kwargs: Any) -> None:
            super().__init__()
            built.append(kwargs)

    config: dict[str, Any] = {
        "identity": {"ui_language": "en"},
        "pet": {"default_monitor": "right", "fps_target": 75, "fps_in_game": 12},
    }
    app = ShellApp(
        runtime_dir=make_runtime(tmp_path),
        config=config,
        bridge_factory=fake_bridge_factory[0],
        enable_hotkeys=False,
        open_url=lambda _url: None,
        pet_window_factory=FakeWindow,
    )
    assert built[0]["default_monitor"] == "right"
    assert isinstance(app.pet, FakeWindow)
    assert app.pet.frames[-1] == {"fpsTarget": 75, "fpsInGame": 12, "gameActive": False}
