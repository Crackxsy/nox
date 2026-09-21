"""Async plumbing every long-running part of the system needs: closing async iterators, awaiting
a value that may or may not be a coroutine, sleeping until a stop event, and running a poll loop
that survives a failing poll.

These exist once so the worker, the sensors, the plugins and the AI router behave identically
instead of each hand-rolling the same five lines with a different set of bugs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.logging import get_logger

log = get_logger(__name__)


async def maybe_await(value: Any) -> Any:
    """Return `value`, awaiting it first if it is awaitable.

    Plugin entry points and callbacks may be written as either `def` or `async def`; this is the
    one place that decides how to call them.
    """
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


async def aclose(iterator: Any) -> None:
    """Close an async iterator that supports `aclose()`; a plain iterator is left alone.

    Generators hold the synthesis/stream thread open until they are closed, so every path that
    abandons one mid-stream calls this.
    """
    closer = getattr(iterator, "aclose", None)
    if closer is None:
        return
    await closer()


async def wait_or_stop(stop: asyncio.Event, timeout: float) -> bool:
    """Sleep up to `timeout` seconds. Returns True when `stop` was set (the caller should exit).

    The plain alternative - `asyncio.sleep(timeout)` - keeps a shutdown waiting for the full
    interval, which is why every backoff and heartbeat loop uses this instead.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def poll_loop[T](
    poll: Callable[[], Awaitable[T]],
    interval: float | Callable[[T | None], float],
    *,
    name: str,
    on_error: Callable[[BaseException], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Call `poll` forever, `interval` seconds apart, logging (never swallowing silently) failures.

    A failing poll does not end the loop - the next cycle tries again - but it is logged with the
    loop name and handed to `on_error`, so the owning sensor can report itself as degraded instead
    of serving a frozen history as if it were live. `interval` may be a callable that derives the
    next delay from the last result (adaptive sampling).
    """
    while True:
        result: T | None = None
        try:
            result = await poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the loop
            log.warning("poll_loop.failed", loop=name, error=f"{type(exc).__name__}: {exc}")
            if on_error is not None:
                on_error(exc)
        else:
            if on_success is not None:
                on_success()
        delay = interval(result) if callable(interval) else interval
        await asyncio.sleep(delay)
