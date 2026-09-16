"""Fire-and-forget event publishing for synchronous security code paths (Event Model, events.py).

`PermissionEngine.check()` and `AuditLog.append()` are synchronous by contract while
`EventBus.publish()`
is a coroutine. `publish_nowait()` schedules the publish on the running loop and keeps a strong
reference to the task until it finishes. Outside a running loop the event is dropped with a debug
log entry; the audit row (SQLite) remains the source of truth in that case.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from nox.core.events import Event, EventBus
from nox.security._logging import get_logger

log = get_logger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()


def publish_nowait(
    bus: EventBus | None,
    name: str,
    payload: BaseModel | dict[str, Any],
    *,
    source: str = "security",
    corr: str | None = None,
) -> bool:
    """Schedule `bus.publish(Event(...))` without awaiting. Returns True when scheduled."""
    if bus is None:
        return False
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    event = Event(name=name, payload=data, source=source, corr=corr)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("security.event_dropped_no_loop", event_name=name)
        return False
    task = loop.create_task(bus.publish(event))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return True


async def publish(
    bus: EventBus | None,
    name: str,
    payload: BaseModel | dict[str, Any],
    *,
    source: str = "security",
    corr: str | None = None,
) -> None:
    """Await `bus.publish(...)`; a failing bus never propagates into security code."""
    if bus is None:
        return
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    try:
        await bus.publish(Event(name=name, payload=data, source=source, corr=corr))
    except Exception:  # noqa: BLE001 - security paths must not fail because a handler failed
        log.warning("security.event_publish_failed", event_name=name)


async def drain_pending() -> None:
    """Wait for events scheduled with `publish_nowait` (used by tests and orderly shutdown)."""
    pending = [t for t in _background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
