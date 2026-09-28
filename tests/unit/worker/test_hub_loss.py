"""HubLossGuard: a worker cut off from the core ends itself instead of living on as a zombie."""

from __future__ import annotations

import asyncio

from nox.ipc.errors import ERR_AUTH_DENIED, IpcError
from nox.worker.hub_loss import HubLossGuard


class ManualSleep:
    """A `sleep` that only returns when the test says the deadline has passed."""

    def __init__(self) -> None:
        self.requested: list[float] = []
        self._release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)
        await self._release.wait()

    def expire(self) -> None:
        self._release.set()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_connection_gone_past_the_deadline_ends_the_worker() -> None:
    sleep = ManualSleep()
    lost: list[str] = []
    guard = HubLossGuard(lost.append, deadline_s=30.0, sleep=sleep)

    guard.connection_changed(False)
    await _settle()
    assert lost == [] and sleep.requested == [30.0]
    sleep.expire()
    await _settle()

    assert guard.fired
    assert lost == ["no connection to the core for 30 s"]


async def test_a_reconnect_within_the_deadline_disarms_it() -> None:
    sleep = ManualSleep()
    lost: list[str] = []
    guard = HubLossGuard(lost.append, deadline_s=30.0, sleep=sleep)

    guard.connection_changed(False)
    await _settle()
    guard.connection_changed(True)
    sleep.expire()
    await _settle()

    assert lost == [] and not guard.fired


async def test_a_refused_reconnect_ends_the_worker_at_once() -> None:
    lost: list[str] = []
    guard = HubLossGuard(lost.append, deadline_s=30.0, sleep=ManualSleep())
    guard.connection_changed(False)

    guard.refused(IpcError(ERR_AUTH_DENIED, "invalid token"))
    await _settle()

    assert len(lost) == 1 and "refused" in lost[0]


async def test_it_fires_only_once() -> None:
    lost: list[str] = []
    guard = HubLossGuard(lost.append, deadline_s=1.0, sleep=ManualSleep())
    guard.refused(IpcError(ERR_AUTH_DENIED, "x"))
    guard.refused(IpcError(ERR_AUTH_DENIED, "x"))
    guard.connection_changed(False)
    await _settle()
    assert len(lost) == 1
