"""Running a short check off the event loop with a time limit - on the core and on a node.

The gate's and the core's text checks are linear and capped, but they must never stall the loop
that carries the stop button and the dead-man rule, whatever text they are given.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

__all__ = ["CHECK_TIMEOUT_S", "off_loop"]

#: The most time a text check (the gate, a write's content) may take before the call is refused.
#: The checks are linear and capped, so this is never reached by honest input.
CHECK_TIMEOUT_S = 2.0


async def off_loop(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """`func(*args, **kwargs)` in a worker thread with `CHECK_TIMEOUT_S`: the event loop - and
    with it the stop button and the dead-man rule - keeps running whatever the text is.
    Raises `TimeoutError` when the time is up."""
    return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), CHECK_TIMEOUT_S)
