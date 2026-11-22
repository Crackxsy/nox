"""StreamResponder: turns a qualifying Twitch chat message into a spoken-in-chat answer.

Triggers on `twitch.chat_message` when `addressed_to_nox` or `relevance >= stream.relevance.
threshold` - except for a line that starts with `!`: a command is answered by whoever owns it (the
Twitch plugin for Nox's own, another bot for theirs), so it only reaches the model when the
classifier marked it as a request to Nox itself (`!nox ...`). Subject to a per-channel cooldown,
a per-viewer cooldown and a max-replies-per-minute cap (all `stream.relevance.*`), and is silenced
entirely once `security.kill_switch` is engaged (safe mode: no AI requests, no plugin actions -
Runtime Lifecycle "Safe mode").

The limits count every *attempt*, not only the answers that reach chat: the model call is the cost,
and a line the plugin refuses (output filter, moderation gate, send rate limit) must not buy the
next chat message a free model call - otherwise one viewer could keep the model busy one call
after the other.

The answer comes from one non-streaming `Router.complete` turn built with
`build_system_prompt(DECIDED_PERSONALITY_BLOCK, facts)` plus the chat message wrapped by
`untrusted(text, f"twitch:{user}")` (Tool Model prompt rules: chat content is data, never
instructions). The answer is trimmed to one line and sent *only* through
`ToolExecutor.call("twitch.chat.send",...)` - never any other path - so it goes through the normal
permission/audit pipeline like every other tool action.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from nox.ai.base import AiRequest, AiRole, Message, Router
from nox.ai.prompting import DECIDED_PERSONALITY_BLOCK, build_system_prompt, untrusted
from nox.core.config import RelevanceConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.security.model import KillSwitch
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

Clock = Callable[[], datetime]
Unsubscribe = Callable[[], None]
FactsFn = Callable[[], Mapping[str, object]]

#: One chat line (the cadence rules; Twitch's own message cap is ~500 chars, this stays clear
#: of it with room for the platform to add its own prefixes).
MAX_REPLY_CHARS = 400
#: Upper bound for the per-viewer cooldown table before entries that are over are dropped.
MAX_TRACKED_VIEWERS = 1024


class StreamResponder:
    def __init__(
        self,
        bus: EventBus,
        router: Router,
        executor: ToolExecutor,
        config: RelevanceConfig,
        killswitch: KillSwitch,
        *,
        facts: FactsFn | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._bus = bus
        self._router = router
        self._executor = executor
        self._config = config
        self._killswitch = killswitch
        self._facts: FactsFn = facts or (lambda: {})
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._last_reply_by_channel: dict[str, datetime] = {}
        self._last_reply_by_viewer: dict[str, datetime] = {}
        self._reply_timestamps: list[datetime] = []
        self._unsub: Unsubscribe | None = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.TWITCH_CHAT_MESSAGE, self._on_message)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # ---- gating -------------------------------------------------------------------------------

    def _qualifies(self, payload: dict[str, Any], text: str) -> bool:
        addressed = bool(payload.get("addressed_to_nox", False))
        if text.startswith("!"):
            # Never lean on the relevance score here: "!lurk why?" scores like a question.
            return addressed
        if addressed:
            return True
        relevance = payload.get("relevance", 0.0)
        return float(relevance or 0.0) >= self._config.threshold

    def _cooldown_ok(self, channel: str, now: datetime) -> bool:
        last = self._last_reply_by_channel.get(channel)
        return last is None or (now - last).total_seconds() >= self._config.channel_cooldown_s

    def _viewer_ok(self, viewer_id: str, now: datetime) -> bool:
        last = self._last_reply_by_viewer.get(viewer_id)
        return last is None or (now - last).total_seconds() >= self._config.viewer_cooldown_s

    def _rate_ok(self, now: datetime) -> bool:
        window_start = now - timedelta(seconds=60)
        self._reply_timestamps = [t for t in self._reply_timestamps if t >= window_start]
        return len(self._reply_timestamps) < self._config.max_replies_per_minute

    def _record_attempt(self, channel: str, viewer_id: str, now: datetime) -> None:
        self._last_reply_by_channel[channel] = now
        self._last_reply_by_viewer[viewer_id] = now
        self._reply_timestamps.append(now)
        if len(self._last_reply_by_viewer) > MAX_TRACKED_VIEWERS:
            self._forget_old_viewers(now)

    def _forget_old_viewers(self, now: datetime) -> None:
        """Viewers whose cooldown is over need no entry; keeps the table from growing for ever."""
        horizon = self._config.viewer_cooldown_s
        self._last_reply_by_viewer = {
            viewer: at
            for viewer, at in self._last_reply_by_viewer.items()
            if (now - at).total_seconds() < horizon
        }

    # ---- event handler --------------------------------------------------------------------------

    async def _on_message(self, ev: Event) -> None:
        if self._killswitch.is_engaged():
            return
        payload = ev.payload
        text = str(payload.get("text", "")).strip()
        if not text or not self._qualifies(payload, text):
            return
        channel = str(payload.get("channel", "public"))
        viewer_id = str(payload.get("viewer_id", ""))
        now = self._clock()
        if (
            not self._cooldown_ok(channel, now)
            or not self._viewer_ok(viewer_id, now)
            or not self._rate_ok(now)
        ):
            log.debug("stream.responder_throttled", channel=channel)
            return
        # Counted before the model is asked: a slow call must not let the next message in, and an
        # empty or refused answer used the model all the same.
        self._record_attempt(channel, viewer_id, now)
        answer = await self._answer(viewer_id, text)
        if not answer:
            return
        result = await self._executor.call(
            agent="nox.stream",
            name="twitch.chat.send",
            arguments={"text": answer},
            mode="stream",
        )
        if not result.ok:
            log.info("stream.responder_send_failed", error=result.error)

    async def _answer(self, viewer_id: str, text: str) -> str:
        system = build_system_prompt(DECIDED_PERSONALITY_BLOCK, dict(self._facts()))
        provenance = f"twitch:{viewer_id or 'unknown'}"
        request = AiRequest(
            request_id=uuid.uuid4().hex,
            role=AiRole.CHAT,
            messages=[
                Message(role="system", content=system),
                Message(role="user", content=untrusted(text, provenance)),
            ],
            mode="stream",
            stream=False,
        )
        response = await self._router.complete(request)
        return _one_line(response.text, MAX_REPLY_CHARS)


def _one_line(text: str, max_chars: int) -> str:
    """Collapse to a single chat line and cut to `max_chars` (one chat message)."""
    stripped = text.strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0].strip()
    if len(line) > max_chars:
        line = line[: max_chars - 1].rstrip() + "…"
    return line
