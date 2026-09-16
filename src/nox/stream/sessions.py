"""StreamSessionService: session lifecycle bridge to the `stream_sessions`/`chat_events` tables
(Spec v0.2 §7/§8, EPIC-11) - opens/closes DB rows on `stream.started`/`stream.ended`, persists
inbound Twitch chat with retention driven by `stream.chat.retain_raw_text_days`, tracks the
connected-plugin/scene picture from `obs.*`/`twitch.*` events, and answers the
`stream.session.status` IPC request (dashboard/shell).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nox.core.config import StreamChatConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.stream_repos import (
    ChatEventRepository,
    StreamSessionRepository,
    ViewerRepository,
    default_chat_retain_until,
)

log = get_logger(__name__)

Unsubscribe = Callable[[], None]


class StreamSessionService:
    """Owns no I/O of its own beyond the two repositories; `start()`/`stop()` (un)subscribe from
    the bus, matching the lifecycle of every other core service wired in `nox.app`."""

    def __init__(
        self,
        bus: EventBus,
        sessions: StreamSessionRepository,
        chat_events: ChatEventRepository,
        chat_config: StreamChatConfig,
        viewers: ViewerRepository | None = None,
    ) -> None:
        self._bus = bus
        self._sessions = sessions
        self._chat = chat_events
        self._chat_config = chat_config
        self._viewers = viewers
        self._unsubs: list[Unsubscribe] = []
        self._active_session_id: int | None = None
        self._started_at: datetime | None = None
        self._scene: str = ""
        self._plugin_status: dict[str, str] = {"obs": "unknown", "twitch": "unknown"}

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.STREAM_STARTED, self._on_started),
            self._bus.subscribe(E.STREAM_ENDED, self._on_ended),
            self._bus.subscribe(E.TWITCH_CHAT_MESSAGE, self._on_chat_message),
            self._bus.subscribe(E.OBS_SCENE_CHANGED, self._on_scene_changed),
            self._bus.subscribe(E.OBS_CONNECTED, self._on_obs_connected),
            self._bus.subscribe(E.OBS_DISCONNECTED, self._on_obs_disconnected),
            self._bus.subscribe(E.TWITCH_CONNECTED, self._on_twitch_connected),
            self._bus.subscribe(E.TWITCH_DISCONNECTED, self._on_twitch_disconnected),
        ]
        # Resume an already-open session across a core restart mid-stream rather than losing track
        # of it (the plugins will re-announce `obs.connected`/`twitch.connected` on their own).
        row = self._sessions.active()
        if row is not None:
            self._active_session_id = row.id
            self._started_at = row.started_at

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @property
    def active_session_id(self) -> int | None:
        return self._active_session_id

    def is_active(self) -> bool:
        return self._active_session_id is not None

    # ---- event handlers --------------------------------------------------------------------

    async def _on_started(self, ev: Event) -> None:
        mode = str(ev.payload.get("mode", "live"))
        row = self._sessions.start(mode=mode)
        self._active_session_id = row.id
        self._started_at = row.started_at
        if ev.payload.get("obs_connected"):
            self._plugin_status["obs"] = "connected"
        if ev.payload.get("twitch_connected"):
            self._plugin_status["twitch"] = "connected"
        log.info("stream.session_opened", session_id=row.id, mode=mode)

    async def _on_ended(self, ev: Event) -> None:
        if self._active_session_id is None:
            return
        reason = str(ev.payload.get("ended_reason", "manual"))
        summary = str(ev.payload.get("summary", ""))
        self._sessions.end(self._active_session_id, ended_reason=reason, summary=summary)
        log.info("stream.session_closed", session_id=self._active_session_id, reason=reason)
        self._active_session_id = None
        self._started_at = None

    async def _on_chat_message(self, ev: Event) -> None:
        viewer_id = ev.payload.get("viewer_id") or None
        # `chat_events.viewer_id` is a foreign key into `viewers` (0002_stream.sql): a viewer
        # tracking service is expected to have seen this viewer already (`stream.viewer_seen`),
        # but upsert defensively here too so a chat message is never dropped for lack of one.
        if viewer_id is not None and self._viewers is not None:
            self._viewers.touch(viewer_id)
        days = self._chat_config.retain_raw_text_days
        if days <= 0:
            # Metadata only (Spec v0.2 §7): keep the row (viewer/session/timestamp) for counts and
            # moderation history, but never persist the raw text itself.
            stored_text = ""
            retain_until = None
        else:
            stored_text = str(ev.payload.get("text", ""))
            retain_until = default_chat_retain_until(days)
        self._chat.add(
            session_id=self._active_session_id,
            kind="message",
            viewer_id=viewer_id,
            text=stored_text,
            retain_until=retain_until,
        )
        if self._active_session_id is not None:
            self._sessions.record_chat_message(self._active_session_id)

    async def _on_scene_changed(self, ev: Event) -> None:
        self._scene = str(ev.payload.get("current_scene", ""))

    async def _on_obs_connected(self, _: Event) -> None:
        self._plugin_status["obs"] = "connected"

    async def _on_obs_disconnected(self, _: Event) -> None:
        self._plugin_status["obs"] = "disconnected"

    async def _on_twitch_connected(self, _: Event) -> None:
        self._plugin_status["twitch"] = "connected"

    async def _on_twitch_disconnected(self, _: Event) -> None:
        self._plugin_status["twitch"] = "disconnected"

    # ---- reads --------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """`stream.session.status {}` response body (roles shell/dashboard, see `nox.app`)."""
        return {
            "active": self._active_session_id is not None,
            "session_id": (
                str(self._active_session_id) if self._active_session_id is not None else None
            ),
            "started_at": self._started_at.isoformat() if self._started_at is not None else None,
            "scene": self._scene or None,
            "plugins": dict(self._plugin_status),
        }
