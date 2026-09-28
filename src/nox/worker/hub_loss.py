"""A worker that has lost the core, and cannot get it back, ends itself.

The IPC client reconnects on its own with the worker's reconnect credential. What it cannot do is
decide when to stop trying: a worker whose core refuses it (a new core, which never honours an old
core's credential) or that stays cut off past its deadline would otherwise live on as a zombie -
still holding a microphone, a chat connection or a smart-home socket, and still looking alive to
anyone who only checks the process. `HubLossGuard` makes that decision: it calls `on_lost` once
the connection has been gone for `deadline_s`, or at once when the core refuses the reconnect. The
worker then shuts down and exits non-zero, and whoever spawned it applies its own restart policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from nox.core.logging import get_logger
from nox.ipc.errors import IpcError

log = get_logger(__name__)

__all__ = ["EXIT_HUB_LOST", "HubLossGuard"]

#: Exit code of a worker that ended itself because it could not reach the core (EX_TEMPFAIL).
EXIT_HUB_LOST = 75


class HubLossGuard:
    """Calls `on_lost(reason)` once, when the hub connection is gone for good."""

    def __init__(
        self,
        on_lost: Callable[[str], None],
        *,
        deadline_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._on_lost = on_lost
        self._deadline_s = deadline_s
        self._sleep = sleep
        self._timer: asyncio.Task[None] | None = None
        self._fired = False

    @property
    def fired(self) -> bool:
        return self._fired

    def connection_changed(self, connected: bool) -> None:
        """The IPC client's connection callback: arm on a loss, disarm on a reconnect."""
        if connected:
            self.cancel()
            return
        if self._fired or (self._timer is not None and not self._timer.done()):
            return
        self._timer = asyncio.get_running_loop().create_task(
            self._expire(), name="nox-hub-loss-deadline"
        )

    def refused(self, error: IpcError) -> None:
        """The core refused to take the worker back: there is nothing left to wait for."""
        self.cancel()
        self._fire(f"the core refused the reconnect ({error.message})")

    def cancel(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _expire(self) -> None:
        await self._sleep(self._deadline_s)
        self._fire(f"no connection to the core for {self._deadline_s:g} s")

    def _fire(self, reason: str) -> None:
        if self._fired:
            return
        self._fired = True
        log.error("worker.hub_lost", reason=reason, note="exiting so the core can start a new one")
        self._on_lost(reason)
