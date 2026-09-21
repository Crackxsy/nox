"""Exponential reconnect backoff, shared by every plugin that holds a long-lived connection.

Three plugins used to carry their own copy of the same twenty lines, each with a different set of
bugs - the worst of which was silence: a permanently wrong password looped forever at maximum
backoff without a single log line. This helper owns the delay, the failure streak and the logging,
so every transport reports the same way and a human can see why nothing is connecting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from nox.core.logging import get_logger
from nox.util.aio import wait_or_stop

log = get_logger(__name__)


@dataclass
class ReconnectBackoff:
    """Delay between connection attempts, plus the failure state a health check can read.

    `name` is the transport ("obs", "twitch", "telegram") and appears in every log line.
    """

    name: str
    min_s: float = 1.0
    max_s: float = 30.0
    #: Delay that will be used before the next attempt.
    current_s: float = field(init=False, default=0.0)
    #: Consecutive failures since the last successful connection.
    failures: int = field(init=False, default=0)
    last_error: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.current_s = self.min_s

    def succeeded(self) -> None:
        """A connection came up: reset the delay, and say so if we had been failing."""
        if self.failures:
            log.info("plugin.reconnected", transport=self.name, after_failures=self.failures)
        self.failures = 0
        self.last_error = ""
        self.current_s = self.min_s

    def failed(self, error: BaseException | str) -> None:
        """Record a failed attempt and log it - the first one, and every time we hit the ceiling.

        Logging every attempt of a long outage would drown the log; logging none of them is how a
        wrong password stays invisible for hours.
        """
        reason = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.last_error = reason
        self.failures += 1
        at_ceiling = self.current_s >= self.max_s
        if self.failures == 1 or at_ceiling:
            log.warning(
                "plugin.connection_failed",
                transport=self.name,
                error=reason,
                failures=self.failures,
                retry_in_s=round(self.current_s, 1),
            )

    async def sleep(self, stop: asyncio.Event) -> bool:
        """Wait out the current delay, then double it. True when `stop` was set meanwhile."""
        delay = self.current_s
        self.current_s = min(self.current_s * 2, self.max_s)
        return await wait_or_stop(stop, delay)


__all__ = ["ReconnectBackoff"]
