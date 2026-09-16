"""`RateLimiter` for `telegram.send` (Spec v0.8 §5.2). Same shape as the twitch plugin's limiter:
at most `max_messages` per `window_s` and at least `min_gap_s` between two sends. Defaults
(20 msg/min, >=1 s gap) stay far under the Bot API's own limits and keep a looping caller from
turning the paired phone into a notification firehose.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        max_messages: int = 20,
        window_s: float = 60.0,
        min_gap_s: float = 1.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_messages = max_messages
        self._window_s = window_s
        self._min_gap_s = min_gap_s
        self._clock = clock or time.monotonic
        self._sent: list[float] = []
        self._last_sent: float | None = None

    def check(self) -> tuple[bool, str]:
        """Would a send be allowed right now? Records nothing."""
        now = self._clock()
        if self._last_sent is not None and (now - self._last_sent) < self._min_gap_s:
            return False, f"minimum gap of {self._min_gap_s}s between messages not met"
        sent_in_window = [t for t in self._sent if now - t < self._window_s]
        if len(sent_in_window) >= self._max_messages:
            return False, (
                f"rate limit exceeded: {self._max_messages} messages per {self._window_s}s"
            )
        return True, ""

    def try_acquire(self) -> tuple[bool, str]:
        """Atomic check-and-record: a send is only recorded when it was actually allowed."""
        ok, reason = self.check()
        if not ok:
            return ok, reason
        now = self._clock()
        self._sent = [t for t in self._sent if now - t < self._window_s]
        self._sent.append(now)
        self._last_sent = now
        return True, ""
