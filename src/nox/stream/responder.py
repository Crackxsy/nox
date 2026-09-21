"""StreamResponder: turns a qualifying Twitch chat message into a spoken-in-chat answer.

Triggers on `twitch.chat_message` when `addressed_to_nox` or `relevance >= stream.relevance.
threshold`, subject to a per-channel cooldown and a max-replies-per-minute cap (both
`stream.relevance.*`), and is silenced entirely once `security.kill_switch` is engaged (safe mode:
no AI requests, no plugin actions - Runtime Lifecycle "Safe mode"). The answer comes from one non-
streaming `Router.complete` turn built with `build_system_prompt(DECIDED_PERSONALITY_BLOCK, facts)`
plus the chat message wrapped by `untrusted(text, f"twitch:{user}")` (Tool Model prompt rules: chat
content is data, never instructions). The answer is trimmed to one line and sent
*only* through `ToolExecutor.call("twitch.chat.send",...)` - never any other path - so it goes
through the normal permission/audit pipeline like every other tool action.
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

#: One chat line (FR-9.x cadence rules; Twitch's own message cap is ~500 chars, this stays clear
#: of it with room for the platform to add its own prefixes).
MAX_REPLY_CHARS = 400


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
        self._reply_timestamps: list[datetime] = []
        self._unsub: Unsubscribe | None = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.TWITCH_CHAT_MESSAGE, self._on_message)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # ---- gating -------------------------------------------------------------------------------

    def _qualifies(self, payload: dict[str, Any]) -> bool:
        if bool(payload.get("addressed_to_nox", False)):
            return True
        relevance = payload.get("relevance", 0.0)
        return float(relevance or 0.0) >= self._config.threshold

    def _cooldown_ok(self, channel: str, now: datetime) -> bool:
        last = self._last_reply_by_channel.get(channel)
        return last is None or (now - last).total_seconds() >= self._config.channel_cooldown_s

    def _rate_ok(self, now: datetime) -> bool:
        window_start = now - timedelta(seconds=60)
        self._reply_timestamps = [t for t in self._reply_timestamps if t >= window_start]
        return len(self._reply_timestamps) < self._config.max_replies_per_minute

    def _record_reply(self, channel: str, now: datetime) -> None:
        self._last_reply_by_channel[channel] = now
        self._reply_timestamps.append(now)

    # ---- event handler --------------------------------------------------------------------------

    async def _on_message(self, ev: Event) -> None:
        if self._killswitch.is_engaged():
            return
        payload = ev.payload
        if not self._qualifies(payload):
            return
        text = str(payload.get("text", "")).strip()
        if not text:
            return
        channel = str(payload.get("channel", "public"))
        now = self._clock()
        if not self._cooldown_ok(channel, now) or not self._rate_ok(now):
            log.debug("stream.responder_throttled", channel=channel)
            return
        viewer_id = str(payload.get("viewer_id", ""))
        answer = await self._answer(viewer_id, text)
        if not answer:
            return
        result = await self._executor.call(
            agent="nox.stream",
            name="twitch.chat.send",
            arguments={"text": answer},
            mode="stream",
        )
        if result.ok:
            self._record_reply(channel, now)
        else:
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
