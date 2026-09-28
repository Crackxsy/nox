"""The heartbeat every worker process sends to the core, in one place.

Besides liveness it carries what the worker knows about its own health (`health`, by component);
the core keeps the latest report, maps it onto its health checks, and treats a report that has
stopped arriving as stale. A failing send is logged and retried rather than escalated: the
supervisor, not the worker, decides what a dead worker means.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.util.aio import wait_or_stop

log = get_logger(__name__)

HealthReport = Callable[[], Awaitable[dict[str, dict[str, str]]]]


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
    health: HealthReport | None = None,
    log_event: str = "worker.heartbeat_failed",
    **log_context: str,
) -> None:
    """Send `worker.heartbeat` every `interval_s` seconds until `stop` is set.

    `load` returning None omits the field instead of inventing a number: a worker that cannot
    measure its own load says nothing rather than reporting a perfectly idle process. `health`,
    when given, adds `{component: {"status": ..., "reason": ...}}`.
    """
    while not stop.is_set():
        payload: dict[str, Any] = {"status": status()}
        current_load = load()
        if current_load is not None:
            payload["load"] = float(current_load)
        if health is not None:
            try:
                report = await health()
            except Exception as exc:  # noqa: BLE001 - liveness must not depend on the report
                log.warning("worker.health_report_failed", error=f"{type(exc).__name__}: {exc}")
                report = {}
            if report:
                payload["health"] = report
        try:
            await client.request("worker.heartbeat", payload, timeout=interval_s)
        except Exception as exc:  # noqa: BLE001 - the core treats a silent worker as stale
            log.warning(log_event, error=str(exc), **log_context)
        if await wait_or_stop(stop, interval_s):
            return
