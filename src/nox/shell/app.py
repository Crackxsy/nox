"""Shell composition root (Component Model: `nox.shell.app` wires everything).

Flow: read runtime files -> create IPC bridge (role shell) -> pet window + tray + hotkeys ->
ping loop decides `connected` -> events update ShellModel -> tray/pet refresh. Every user action
is a typed request; when the core is offline the kill switch goes to the supervisor control port.
"Quit" (B-6) always goes through the supervisor (`sup.stop`), not just a local exit: the supervisor
stops the core gracefully, then the shell process, then itself.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from nox.core.state import PrivacyMode
from nox.shell.dialogs import PermissionDialog
from nox.shell.hotkeys import GlobalHotkeys, HotkeyTracker
from nox.shell.ipc_bridge import BridgeFactory, BridgeUnavailableError, IpcBridge, create_bridge
from nox.shell.logic import (
    HotkeyAction,
    ShellModel,
    dashboard_url,
    hotkey_map,
    kill_path,
    pet_url,
    toggle_privacy,
    ws_url,
)
from nox.shell.logutil import get_logger
from nox.shell.pet_window import PetWindow
from nox.shell.runtime import (
    IpcEndpoints,
    ShellState,
    load_config,
    load_shell_state,
    read_ipc_endpoints,
    read_session_token,
    read_supervisor_token,
    resolve_runtime_dir,
    save_shell_state,
)
from nox.shell.supervisor_client import (
    SupervisorUnavailableError,
    send_supervisor_kill,
    send_supervisor_stop,
)
from nox.shell.tray import TrayController

log = get_logger(__name__)

SUBSCRIPTIONS = ["security.*", "privacy.*", "system.*", "voice.*", "state.changed"]
PING_INTERVAL_MS = 5000
RECONNECT_INTERVAL_MS = 3000
PING_FAILURES_BEFORE_RECONNECT = 2
PING_TIMEOUT_S = 3.0
CLIENT_ID = "shell:main"

PermissionHandler = Callable[[dict[str, Any]], dict[str, Any] | None]
SupervisorKill = Callable[[str, int, str], dict[str, Any]]
SupervisorStop = Callable[[str, int, str], dict[str, Any]]


class _Signals(QObject):
    event_received = Signal(object)  # envelope dict, emitted from the bridge thread
    hotkey = Signal(object, bool)  # (HotkeyAction, pressed), emitted from pynput thread
    ping = Signal(bool)


class ShellApp:
    """Runs inside an existing QApplication. Construct, `start()`, then run the Qt loop."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        config: dict[str, Any] | None = None,
        bridge_factory: BridgeFactory = create_bridge,
        permission_handler: PermissionHandler | None = None,
        supervisor_kill: SupervisorKill | None = None,
        supervisor_stop: SupervisorStop | None = None,
        open_url: Callable[[str], object] = webbrowser.open,
        enable_hotkeys: bool = True,
        create_pet_window: bool = True,
    ) -> None:
        self.runtime_dir = runtime_dir or resolve_runtime_dir()
        self.config = config if config is not None else load_config()
        self.language = str(self.config.get("identity", {}).get("ui_language", "de"))
        self.model = ShellModel()
        self.state: ShellState = load_shell_state(self.runtime_dir)
        self.model.pet_visible = self.state.visible
        self.model.click_through = self.state.click_through

        self.token: str | None = read_session_token(self.runtime_dir)
        self.endpoints: IpcEndpoints | None = read_ipc_endpoints(self.runtime_dir)
        self.bridge: IpcBridge | None = None
        self._bridge_factory = bridge_factory
        self._permission_handler = permission_handler or self._native_permission_dialog
        self._supervisor_kill = supervisor_kill or self._default_supervisor_kill
        self._supervisor_stop = supervisor_stop or self._default_supervisor_stop
        self._open_url = open_url
        self._session_permissions: dict[tuple[str, str, str], bool] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []  # last requests (debug/tests)

        self._signals = _Signals()
        self._signals.event_received.connect(self._on_event_main)
        self._signals.hotkey.connect(self._on_hotkey_main)
        self._signals.ping.connect(self._on_ping_result)

        self.tray = TrayController(
            on_toggle_pet=self.toggle_pet,
            on_privacy=self.set_privacy,
            on_mute=self.toggle_mute,
            on_dashboard=self.open_dashboard,
            on_kill=self._kill_from_tray,
            on_quit=self.quit,
            on_click_through=self.set_click_through,
            language=self.language,
        )
        self.pet: PetWindow | None = None
        if create_pet_window:
            self.pet = PetWindow(self.state, on_moved=self._on_pet_moved)

        self._hotkeys: GlobalHotkeys | None = None
        if enable_hotkeys:
            tracker = HotkeyTracker(hotkey_map(self.config))
            self._hotkeys = GlobalHotkeys(tracker, self._on_hotkey_thread)

        self._ping_timer = QTimer()
        self._ping_timer.setInterval(PING_INTERVAL_MS)
        self._ping_timer.timeout.connect(self._ping)
        # The supervisor restarts the core with a fresh session token; the shell must follow
        # (re-read the runtime files, rebuild the bridge, reload the pet page) instead of
        # staying offline for the rest of the session (2026-09-15).
        self._reconnect_timer = QTimer()
        self._reconnect_timer.setInterval(RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._retry_bridge)
        self._ping_failures = 0
        self._pet_page_stale = True
        self._pet_page_token: str | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        self.tray.show()
        if self.pet is not None:
            self._load_pet_page()
        self._connect_bridge()
        if self.pet is not None:
            if self.model.pet_visible:
                self.pet.show()
            if self.model.click_through:
                self.model.click_through = self.pet.set_click_through(True)
        if self._hotkeys is not None and not self._hotkeys.start():
            self.tray.notify("Nox", "Hotkeys unavailable (pynput missing)")
        self._ping_timer.start()
        self._ping()
        self.tray.refresh(self.model)
        log.info("shell.started", runtime_dir=str(self.runtime_dir), connected=self.model.connected)

    def quit(self) -> None:
        """B-6: ask the supervisor to stop core, shell and itself before this process exits."""
        self._request_supervisor_stop()
        self._ping_timer.stop()
        self._reconnect_timer.stop()
        if self._hotkeys is not None:
            self._hotkeys.stop()
        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:
                log.exception("shell.bridge_stop_failed")
        self._persist_state()
        self.tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _connect_bridge(self) -> None:
        # Re-read on every attempt: the core rewrites session.token and ipc.json at each boot.
        self.token = read_session_token(self.runtime_dir)
        self.endpoints = read_ipc_endpoints(self.runtime_dir)
        if self.token is None or self.endpoints is None:
            log.warning("shell.core_unreachable", reason="session.token or ipc.json missing")
            self._schedule_reconnect()
            return
        try:
            bridge = self._bridge_factory(
                ws_url(self.endpoints.ws_port, self.endpoints.host), self.token, "shell", CLIENT_ID
            )
            bridge.on_event(self._on_event_thread)
            bridge.start()
        except BridgeUnavailableError as exc:
            log.warning("shell.bridge_unavailable", reason=str(exc))
            return  # the client module itself is missing: retrying cannot help
        except Exception as exc:  # noqa: BLE001 - core down or still booting: retry, no traceback
            log.warning("shell.bridge_start_failed", error=str(exc))
            self._schedule_reconnect()
            return
        self.bridge = bridge
        self._ping_failures = 0
        self._reconnect_timer.stop()
        self._subscribe()
        # Shell and core start together: the page may have been loaded with the previous
        # run's token (the pet's own socket is then denied: `ipc_auth_denied role=pet`).
        if self.pet is not None and (self._pet_page_stale or self._pet_page_token != self.token):
            self._load_pet_page()

    def _schedule_reconnect(self) -> None:
        if not self._reconnect_timer.isActive():
            self._reconnect_timer.start()

    def _retry_bridge(self) -> None:
        if self.bridge is None:
            self._connect_bridge()

    def _drop_bridge(self) -> None:
        """The core stopped answering pings: discard the bridge (its token is stale after a core
        restart) and let `_retry_bridge` build a new one from the fresh runtime files."""
        bridge, self.bridge = self.bridge, None
        self._pet_page_stale = True
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("shell.bridge_stop_failed", error=str(exc))
        log.warning("shell.bridge_dropped", ping_failures=self._ping_failures)
        self._schedule_reconnect()

    def _subscribe(self) -> None:
        self._request("ipc.subscribe", {"patterns": SUBSCRIPTIONS})

    def _load_pet_page(self) -> None:
        assert self.pet is not None
        if self.token is not None and self.endpoints is not None:
            variant = str(self.config.get("pet", {}).get("variant", "neutral"))
            self.pet.load(
                pet_url(self.endpoints.http_port, self.token, self.endpoints.host, variant=variant)
            )
            self._pet_page_stale = False
            self._pet_page_token = self.token
        else:
            self.pet.show_offline_page()

    # -- connectivity ----------------------------------------------------------------------------

    def _ping(self) -> None:
        if self.bridge is None:
            self._set_connected(False)
            return
        try:
            fut = self.bridge.call("ipc.ping", {})
        except Exception:
            log.exception("shell.ping_failed")
            self._set_connected(False)
            return
        fut.add_done_callback(self._ping_done)

    def _ping_done(self, fut: Future[Any]) -> None:
        ok = fut.exception(timeout=0) is None if fut.done() else False
        self._signals.ping.emit(ok)

    def _on_ping_result(self, ok: bool) -> None:
        if ok:
            self._ping_failures = 0
        else:
            self._ping_failures += 1
            if self.bridge is not None and self._ping_failures >= PING_FAILURES_BEFORE_RECONNECT:
                self._drop_bridge()
        self._set_connected(ok)

    def _set_connected(self, connected: bool) -> None:
        if self.model.set_connected(connected):
            log.info("shell.connection_changed", connected=connected)
            if connected:
                self._subscribe()
            self.tray.refresh(self.model)

    # -- events ----------------------------------------------------------------------------------

    def _on_event_thread(self, envelope: dict[str, Any]) -> None:
        self._signals.event_received.emit(envelope)

    def _on_event_main(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        self.handle_event(envelope)

    def handle_event(self, envelope: dict[str, Any]) -> None:
        """Main-thread event dispatch (public so tests can drive it directly)."""
        name = str(envelope.get("name", ""))
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            return
        if name == "security.permission_requested":
            self._handle_permission(payload)
            return
        changed = self.model.apply_event(name, payload)
        if changed:
            self.tray.refresh(self.model)
        if name == "security.kill_switch":
            self.tray.notify("Nox", "Kill switch engaged – safe mode", critical=True)

    def _handle_permission(self, payload: dict[str, Any]) -> None:
        key = (str(payload.get("agent")), str(payload.get("tool")), str(payload.get("action")))
        remembered = self._session_permissions.get(key)
        if remembered is not None:
            reply = {
                "grant_id": payload.get("request_id"),
                "decision": "allow" if remembered else "deny",
                "remember": True,
            }
        else:
            answer = self._permission_handler(payload)
            if answer is None:
                return
            reply = answer
            if reply.get("remember"):
                self._session_permissions[key] = reply["decision"] == "allow"
        self._request("security.permission.reply", reply)

    def _native_permission_dialog(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return PermissionDialog(payload, language=self.language).run()
        except ValueError:
            log.warning("shell.permission_request_invalid")
            return None

    # -- actions ---------------------------------------------------------------------------------

    def set_privacy(self, mode: PrivacyMode) -> None:
        self._request("privacy.set", {"mode": mode.value})

    def toggle_mute(self) -> None:
        self._request("voice.mute", {"muted": not self.model.muted})

    def ptt(self, pressed: bool) -> None:
        self._request("voice.ptt", {"pressed": pressed})

    def toggle_pet(self) -> None:
        self.model.pet_visible = not self.model.pet_visible
        if self.pet is not None:
            self.pet.setVisible(self.model.pet_visible)
        self.state.visible = self.model.pet_visible
        self._persist_state()

    def set_click_through(self, enabled: bool) -> None:
        if self.pet is not None:
            enabled = self.pet.set_click_through(enabled)
        self.model.click_through = enabled
        self.state.click_through = enabled
        self._persist_state()
        self.tray.refresh(self.model)

    def open_dashboard(self) -> None:
        if self.token is None or self.endpoints is None:
            self.tray.notify("Nox", "Dashboard unavailable: core offline")
            return
        self._open_url(dashboard_url(self.endpoints.http_port, self.token, self.endpoints.host))

    def kill_switch(self, origin: str) -> str:
        """Kill via core when connected, else via supervisor. Returns the path used."""
        path = kill_path(self.model)
        if path == "core":
            self._request("security.kill", {"reason": "user", "origin": origin})
            return path
        sup_token = read_supervisor_token(self.runtime_dir)
        host = self.endpoints.host if self.endpoints else "127.0.0.1"
        port = (
            self.endpoints.supervisor_port
            if self.endpoints
            else IpcEndpoints.model_fields["supervisor_port"].default
        )
        if sup_token is None:
            log.error("shell.kill_no_supervisor_token")
            self.tray.notify("Nox", "Kill switch failed: no supervisor token", critical=True)
            return "failed"
        try:
            self._supervisor_kill(host, port, sup_token)
        except SupervisorUnavailableError as exc:
            log.error("shell.kill_supervisor_unreachable", reason=str(exc))
            self.tray.notify("Nox", "Kill switch failed: supervisor unreachable", critical=True)
            return "failed"
        return path

    def _kill_from_tray(self) -> None:
        self.kill_switch("tray")

    def _default_supervisor_kill(self, host: str, port: int, token: str) -> dict[str, Any]:
        return send_supervisor_kill(host, port, token, reason="user", origin="shell")

    def _request_supervisor_stop(self) -> None:
        """B-6: "Quit" always asks the supervisor, whether or not the core is reachable."""
        sup_token = read_supervisor_token(self.runtime_dir)
        if sup_token is None:
            log.warning("shell.quit_no_supervisor_token")
            return
        host = self.endpoints.host if self.endpoints else "127.0.0.1"
        port = (
            self.endpoints.supervisor_port
            if self.endpoints
            else IpcEndpoints.model_fields["supervisor_port"].default
        )
        try:
            self._supervisor_stop(host, port, sup_token)
        except SupervisorUnavailableError as exc:
            log.warning("shell.quit_supervisor_unreachable", reason=str(exc))

    def _default_supervisor_stop(self, host: str, port: int, token: str) -> dict[str, Any]:
        return send_supervisor_stop(host, port, token, reason="user_quit")

    # -- hotkeys ---------------------------------------------------------------------------------

    def _on_hotkey_thread(self, action: HotkeyAction, pressed: bool) -> None:
        self._signals.hotkey.emit(action, pressed)

    def _on_hotkey_main(self, action: object, pressed: bool) -> None:
        if isinstance(action, HotkeyAction):
            self.apply_hotkey(action, pressed)

    def apply_hotkey(self, action: HotkeyAction, pressed: bool) -> None:
        if action == HotkeyAction.PTT:
            self.ptt(pressed)
        elif not pressed:
            return
        elif action == HotkeyAction.MUTE:
            self.toggle_mute()
        elif action == HotkeyAction.PRIVACY:
            self.set_privacy(toggle_privacy(self.model))
        elif action == HotkeyAction.KILL:
            self.kill_switch("hotkey")
        elif action == HotkeyAction.TOGGLE_PET:
            self.toggle_pet()

    # -- helpers ---------------------------------------------------------------------------------

    def _request(self, name: str, payload: dict[str, Any]) -> Future[Any] | None:
        self.calls.append((name, payload))
        if self.bridge is None:
            log.warning("shell.request_dropped_offline", name=name)
            return None
        try:
            fut = self.bridge.call(name, payload)
        except Exception:
            log.exception("shell.request_failed", name=name)
            return None
        fut.add_done_callback(lambda f: self._log_result(name, f))
        return fut

    @staticmethod
    def _log_result(name: str, fut: Future[Any]) -> None:
        exc = fut.exception() if fut.done() else None
        if exc is not None:
            log.warning("shell.request_error", name=name, error=type(exc).__name__)

    def _on_pet_moved(self, x: int, y: int) -> None:
        self.state.x, self.state.y = x, y
        self._persist_state()

    def _persist_state(self) -> None:
        if self.pet is not None:
            self.state.x, self.state.y = self.pet.x(), self.pet.y()
        try:
            save_shell_state(self.runtime_dir, self.state)
        except OSError:
            log.warning("shell.state_save_failed")


def run(argv: list[str] | None = None, *, runtime_dir: Path | None = None) -> int:
    import os
    import sys

    # B-7/pet_window.py docstring: force Chromium's software compositor when requested. Must be set
    # before QApplication() is constructed, which is why this lives here and not in pet_window.py.
    if os.environ.get("NOX_PET_SOFTWARE_RENDER") == "1":
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if "--disable-gpu-compositing" not in flags:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --disable-gpu-compositing".strip()

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(argv or sys.argv)
    app.setQuitOnLastWindowClosed(False)
    shell = ShellApp(runtime_dir=runtime_dir)
    shell.start()
    return int(app.exec())
