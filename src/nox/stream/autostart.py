"""Start the stream session when the stream starts - not when someone remembers to say so.

Twitch chat is only read while Nox is in stream mode (the `twitch` plugin runs under the `stream`
profile), so chat went unanswered until someone said "lass uns streamen". Two signals now start it:

* OBS goes live: the `obs` plugin (running in the companion profile as well) reports
  `stream.started`, also when OBS was already live when Nox connected;
* the Twitch channel goes live: `TwitchLiveWatcher` asks the Twitch API every
  `twitch_live_poll_s` while Nox is not in stream mode.

From the modes listed in `stream.auto_session.from_modes` (by default the everyday ones) Nox
switches to stream mode itself and switches back when the stream ends - only if it was the one
that switched. In any other mode - coding, a game - it does not take over; it says that the stream
is live instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.state import Mode

log = get_logger(__name__)

HELIX_STREAMS_URL = "https://api.twitch.tv/helix/streams"

SwitchMode = Callable[[Mode, str], Awaitable[Any]]
Notify = Callable[[str], Awaitable[Any]]


class AutoStreamSession:
    """Turns "the stream is live" into stream mode, and "the stream ended" back again."""

    def __init__(
        self,
        bus: EventBus,
        *,
        current_mode: Callable[[], str],
        switch_mode: SwitchMode,
        notify: Notify,
        from_modes: list[str],
        enabled: bool = True,
    ) -> None:
        self._bus = bus
        self._current_mode = current_mode
        self._switch_mode = switch_mode
        self._notify = notify
        self._from_modes = set(from_modes)
        self._enabled = enabled
        #: The mode Nox left when it switched to stream mode by itself; None when it did not.
        self._switched_from: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

    @property
    def switched_from(self) -> str | None:
        return self._switched_from

    def start(self) -> None:
        if not self._enabled:
            return
        self._unsubs = [
            self._bus.subscribe(E.STREAM_STARTED, self._on_started),
            self._bus.subscribe(E.STREAM_ENDED, self._on_ended),
        ]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    async def _on_started(self, _event: Event) -> None:
        await self.live(source="obs")

    async def _on_ended(self, _event: Event) -> None:
        async with self._lock:
            previous, self._switched_from = self._switched_from, None
            if previous is None or self._current_mode() != Mode.STREAM.value:
                return
            log.info("stream.auto_session_ended", back_to=previous)
            await self._switch_mode(Mode(previous), "stream.ended")

    async def live(self, *, source: str) -> bool:
        """The stream is live (`source`: obs | twitch). Returns whether Nox switched modes."""
        async with self._lock:
            mode = self._current_mode()
            if mode == Mode.STREAM.value:
                return False
            if mode not in self._from_modes:
                log.info("stream.auto_session_skipped", mode=mode, source=source)
                await self._notify(mode)
                return False
            self._switched_from = mode
            log.info("stream.auto_session_started", source=source, previous=mode)
            await self._switch_mode(Mode.STREAM, f"stream.live.{source}")
            return True


ClientFactory = Callable[..., httpx.AsyncClient]
Credentials = Callable[[], tuple[str, str] | None]


class TwitchLiveWatcher:
    """Asks Twitch whether the configured channel is live, while Nox is not streaming.

    Uses the chat bot's own login (any user token may read `/helix/streams`) through the core's
    egress guard, so privacy mode PRIVATE/OFFLINE or a profile without `api.twitch.tv` simply
    stops it. One request per interval, nothing while in stream mode, nothing without a login.
    """

    def __init__(
        self,
        *,
        channel: Callable[[], str],
        credentials: Credentials,
        client_factory: ClientFactory,
        current_mode: Callable[[], str],
        on_live: Callable[[], Awaitable[Any]],
        interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._channel = channel
        self._credentials = credentials
        self._client_factory = client_factory
        self._current_mode = current_mode
        self._on_live = on_live
        self._interval_s = interval_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._was_live = False

    def start(self) -> None:
        if self._interval_s > 0 and self._task is None:
            self._task = asyncio.create_task(self._run(), name="twitch-live-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            await self.check_once()

    async def check_once(self) -> bool | None:
        """One look. True/False: live or not; None: not asked (streaming already, no channel,
        no login, or Twitch could not be reached)."""
        channel = self._channel().strip().lstrip("#").lower()
        creds = self._credentials()
        if not channel or creds is None or self._current_mode() == Mode.STREAM.value:
            self._was_live = False
            return None
        client_id, token = creds
        try:
            async with self._client_factory(timeout=10.0) as client:
                response = await client.get(
                    HELIX_STREAMS_URL,
                    params={"user_login": channel},
                    headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
                )
        except Exception as exc:  # noqa: BLE001 - egress denied, offline: try again later
            log.info("stream.twitch_live_check_failed", error=type(exc).__name__)
            return None
        if response.status_code != 200:
            log.info("stream.twitch_live_check_refused", status=response.status_code)
            return None
        try:
            data = response.json().get("data", [])
        except ValueError:
            return None
        live = any(str(item.get("type", "")) == "live" for item in data if isinstance(item, dict))
        if live and not self._was_live:
            await self._on_live()
        self._was_live = live
        return live
