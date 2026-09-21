"""`RateLimiter` for `twitch.chat.send` (Stream Bot): at most `max_messages` per `window_s`, and at
least `min_gap_s` between any two sends. Defaults (20 msg / 30 s, >=1.5 s gap) stay under Twitch's
own moderator chat limit with margin. Configurable via the plugin's `rate_limit_*` config keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        max_messages: int = 20,
        window_s: float = 30.0,
        min_gap_s: float = 1.5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_messages = max_messages
        self._window_s = window_s
        self._min_gap_s = min_gap_s
        self._clock = clock or time.monotonic
        self._sent: list[float] = []
        self._last_sent: float | None = None

    def check(self) -> tuple[bool, str]:
        """Would a send be allowed right now? Does not record anything."""
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
        """Atomically check-and-record: only records a send when it is actually allowed."""
        ok, reason = self.check()
        if not ok:
            return ok, reason
        now = self._clock()
        self._sent = [t for t in self._sent if now - t < self._window_s]
        self._sent.append(now)
        self._last_sent = now
        return True, ""
