"""The bus contract: what a publisher and a subscriber may rely on.

A protocol rather than an implementation, so a test can supply a recorder and the core can
supply the real bus without either importing the other."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from nox.core.events.base import Event

__all__ = ["EventBus", "Handler"]

Handler = Callable[[Event], Awaitable[None] | None]


class EventBus(Protocol):
    """Async in-process pub/sub. Implementations must be safe to call from any task.

    - subscribe(pattern): glob patterns, e.g. "voice.*" or "*". Returns an unsubscribe callable.
    - publish(event): validates payload against PAYLOAD_MODELS when registered, dispatches to
      handlers, never raises into the publisher because a handler failed (handler errors are
      logged and emitted as system events).
    - Priority: security.* and system.* handlers run before others.
    """

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]: ...
    async def publish(self, event: Event) -> None: ...
    async def wait_for(
        self, name: str, *, timeout: float | None = None, corr: str | None = None
    ) -> Event: ...
