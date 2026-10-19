"""Telling the user when a stream integration stops working - before the stream notices.

* Twitch: a login Twitch no longer accepts ("Invalid refresh token") used to fail silently; the
  chat simply went quiet. `AuthWatch` reports it in the health list and, with the proactive
  service's quiet hours and budget, starts the device-code login itself and shows the code: "open
  twitch.tv/activate, code ABCD-1234". The code is shown, never spoken - Nox's voice may be on
  stream.
* OBS: a wrong or missing websocket password is reported the same way (health + one notice).
* The desktop pet on a captured monitor: when OBS goes live while the desktop pet is open and OBS
  captures a whole monitor, Nox warns once that the pet - and its capture indicator - may be on
  stream, and points at the overlay. Best effort: whether the pet sits on *that* monitor is not
  checked (OBS names monitors by device path, the pet's window by Qt screen; matching them is not
  reliable), so the warning says "may".
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.events import E, Event, EventBus, HealthStatus
from nox.core.logging import get_logger
from nox.settings.twitch_auth import TwitchAuthService, TwitchAuthState
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

#: Do not start another login, or repeat the same notice, more often than this.
RENOTIFY_S = 30 * 60.0

Notify = Callable[[str, str], Awaitable[Any]]  # (context_key, text)


def _text(language: str, de: str, en: str) -> str:
    return de if language.startswith("de") else en


class AuthWatch:
    def __init__(
        self,
        bus: EventBus,
        twitch: TwitchAuthService | None,
        *,
        notify: Notify,
        language: Callable[[], str],
        twitch_enabled: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._twitch = twitch
        self._notify = notify
        self._language = language
        self._twitch_enabled = twitch_enabled
        self._clock = clock
        self._obs_error = ""
        self._last: dict[str, float] = {}
        self._unsubs: list[Callable[[], None]] = []

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.TWITCH_AUTH_CHANGED, self._on_twitch_auth),
            self._bus.subscribe(E.OBS_AUTH_FAILED, self._on_obs_auth_failed),
            self._bus.subscribe(E.OBS_CONNECTED, self._on_obs_connected),
            self._bus.subscribe(E.STREAM_STARTED, self._on_stream_started),
        ]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    # -- health ----------------------------------------------------------------------------------

    async def twitch_health(self) -> tuple[HealthStatus, str]:
        if self._twitch is None:
            return HealthStatus.UNAVAILABLE, "Twitch login service not available"
        state = self._twitch.state
        error = str(self._twitch.status().get("error", ""))
        if state is TwitchAuthState.AUTHORIZED:
            return HealthStatus.AVAILABLE, "signed in"
        if state is TwitchAuthState.EXPIRED:
            return HealthStatus.UNAVAILABLE, "login expired - sign in again (twitch.tv/activate)"
        if state is TwitchAuthState.PENDING:
            return HealthStatus.LIMITED, "waiting for the code to be entered at twitch.tv/activate"
        if state is TwitchAuthState.ERROR:
            return HealthStatus.LIMITED, error or "Twitch could not be reached"
        return HealthStatus.LIMITED, "not signed in to Twitch"

    async def obs_health(self) -> tuple[HealthStatus, str]:
        if self._obs_error:
            return HealthStatus.UNAVAILABLE, self._obs_error
        return HealthStatus.AVAILABLE, "no login problem reported"

    # -- events ----------------------------------------------------------------------------------

    def _due(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < RENOTIFY_S:
            return False
        self._last[key] = now
        return True

    async def _on_twitch_auth(self, event: Event) -> None:
        if event.payload.get("state") == TwitchAuthState.EXPIRED.value:
            await self.relogin_twitch(reason="expired")

    async def _on_stream_started(self, _event: Event) -> None:
        if self._twitch is not None and self._twitch.needs_login:
            await self.relogin_twitch(reason="stream_started")

    async def relogin_twitch(self, *, reason: str) -> bool:
        """Start the device-code login and show its code. Returns whether a code was shown."""
        twitch = self._twitch
        if twitch is None or not self._twitch_enabled() or not twitch.needs_login:
            return False
        if not self._due("twitch.auth"):
            return False
        lang = self._language()
        try:
            started = await twitch.start()
        except Exception as exc:  # noqa: BLE001 - no client id, egress denied, Twitch down
            log.warning("stream.twitch_relogin_failed", error=type(exc).__name__)
            await self._notify(
                "twitch.auth",
                _text(
                    lang,
                    "Twitch-Login abgelaufen - bitte im Dashboard unter Einstellungen neu "
                    "anmelden.",
                    "Twitch login expired - sign in again in the dashboard under Settings.",
                ),
            )
            return False
        uri = str(started.get("verification_uri") or "https://www.twitch.tv/activate")
        code = str(started.get("user_code", ""))
        log.info("stream.twitch_relogin_started", reason=reason)
        await self._notify(
            "twitch.auth",
            _text(
                lang,
                f"Twitch-Login abgelaufen - öffne {uri} und gib den Code {code} ein.",
                f"Twitch login expired - open {uri} and enter the code {code}.",
            ),
        )
        return True

    async def _on_obs_auth_failed(self, event: Event) -> None:
        self._obs_error = "OBS rejected the login: " + str(event.payload.get("reason", ""))
        if not self._due("obs.auth"):
            return
        await self._notify(
            "obs.auth",
            _text(
                self._language(),
                "OBS lehnt die Verbindung ab: das WebSocket-Passwort stimmt nicht. In OBS unter "
                "Werkzeuge > WebSocket-Servereinstellungen nachsehen und in Nox unter "
                "Einstellungen > Zugangsdaten (nox/obs/websocket_password) eintragen.",
                "OBS refuses the connection: the WebSocket password does not match. Look it up in "
                "OBS under Tools > WebSocket Server Settings and enter it in Nox under Settings > "
                "Credentials (nox/obs/websocket_password).",
            ),
        )

    async def _on_obs_connected(self, _event: Event) -> None:
        self._obs_error = ""


class CaptureWarning:
    """Warns once per stream when the desktop pet may be visible in a monitor capture."""

    def __init__(
        self,
        bus: EventBus,
        executor: ToolExecutor,
        *,
        pet_open: Callable[[], bool],
        notify: Notify,
        language: Callable[[], str],
        mode: Callable[[], str],
    ) -> None:
        self._bus = bus
        self._executor = executor
        self._pet_open = pet_open
        self._notify = notify
        self._language = language
        self._mode = mode
        self._warned = False
        self._unsubs: list[Callable[[], None]] = []

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.STREAM_STARTED, self._on_started),
            self._bus.subscribe(E.STREAM_ENDED, self._on_ended),
        ]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    async def _on_started(self, _event: Event) -> None:
        await self.check()

    async def _on_ended(self, _event: Event) -> None:
        self._warned = False

    async def _read(self, request_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
        result = await self._executor.call(
            "nox.stream",
            "obs.request",
            {"request_type": request_type, "request_data": data},
            mode=self._mode(),
            origin="local",
        )
        payload = result.data or {}
        if not result.ok or not payload.get("ok"):
            return None
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    async def check(self) -> bool:
        """Look once; returns whether it warned."""
        if self._warned or not self._pet_open():
            return False
        listed = await self._read("GetInputList", {"inputKind": "monitor_capture"})
        if listed is None:
            return False
        for item in listed.get("inputs", []):
            name = str(item.get("inputName", ""))
            active = await self._read("GetSourceActive", {"sourceName": name})
            if active is not None and active.get("videoActive"):
                self._warned = True
                await self._notify(
                    "stream.pet_on_capture",
                    _text(
                        self._language(),
                        "OBS nimmt gerade einen ganzen Monitor auf. Sitzt das Desktop-Pet darauf, "
                        "sehen die Zuschauer es samt Aufnahme-Anzeige. Besser: im Dashboard "
                        "'OBS-Overlay einrichten'.",
                        "OBS is capturing a whole monitor. If the desktop pet sits on it, viewers "
                        "see it, capture indicator included. Better: 'Set up OBS overlay' in the "
                        "dashboard.",
                    ),
                )
                return True
        return False
