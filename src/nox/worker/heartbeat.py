"""The heartbeat every worker process sends to the core, in one place.

The core marks a worker unavailable after three missed heartbeats, so a failing send is logged and
retried rather than escalated: the supervisor, not the worker, decides what a dead worker means.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.util.aio import wait_or_stop

log = get_logger(__name__)


class HeartbeatClient(Protocol):
    """The one call a heartbeat needs from an IPC client."""

    async def request(
        self, name: str, payload: Any = None, *, timeout: float | None = None
    ) -> dict[str, Any]: ...


async def heartbeat_loop(
    client: HeartbeatClient,
    stop: asyncio.Event,
    *,
    status: Callable[[], str],
    interval_s: float,
    load: Callable[[], float | None] = lambda: None,
    log_event: str = "worker.heartbeat_failed",
    **log_context: str,
) -> None:
    """Send `worker.heartbeat` every `interval_s` seconds until `stop` is set.

    `load` returning None omits the field instead of inventing a number: a worker that cannot
    measure its own load says nothing rather than reporting a perfectly idle process.
    """
    while not stop.is_set():
        payload: dict[str, Any] = {"status": status()}
        current_load = load()
        if current_load is not None:
            payload["load"] = float(current_load)
        try:
            await client.request("worker.heartbeat", payload, timeout=interval_s)
        except Exception as exc:  # noqa: BLE001 - the core marks us unavailable after 3 misses
            log.warning(log_event, error=str(exc), **log_context)
        if await wait_or_stop(stop, interval_s):
            return
