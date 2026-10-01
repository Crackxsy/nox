"""Reporting a blocked event loop, together with the code that blocks it.

Everything in the core shares one asyncio loop, so a single synchronous step that takes seconds -
an import, a read from a busy disk, a query - silences the supervisor heartbeat, the IPC hub and
every timer at once. From the outside that looks like a hang, and the log shows nothing but a gap.

`LoopWatch` is a daemon thread that pings the loop. When a ping stays unanswered for
`threshold_s`, it logs `core.loop_stalled` with the loop thread's current stack, repeats that
every `report_every_s` while the stall lasts, and logs `core.loop_resumed` with the stall's length
once the loop answers again. The stack names modules and functions, never file paths, so the log
does not carry the installation's folder layout.

It only observes: the loop sees one tiny callback per `interval_s` and nothing else.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["LoopWatch", "describe_stack"]


def describe_stack(frame: FrameType | None, *, limit: int = 14) -> list[str]:
    """`module:function:line` for the innermost `limit` frames, outermost first."""
    lines: list[str] = []
    while frame is not None and len(lines) < limit:
        module = frame.f_globals.get("__name__", "?")
        lines.append(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    lines.reverse()
    return lines


class LoopWatch:
    """Construct on the loop's own thread: that thread is the one whose stack gets reported."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold_s: float = 3.0,
        report_every_s: float = 5.0,
        interval_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._threshold = threshold_s
        self._report_every = report_every_s
        self._interval = interval_s
        self._clock = clock
        self._loop_thread_id = threading.get_ident()
        self._answered = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Stalls seen so far; read by tests and available to a health view.
        self.stalls = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._answered = self._clock()
        self._thread = threading.Thread(target=self._run, name="nox-loopwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _answer(self) -> None:
        self._answered = self._clock()

    def _run(self) -> None:
        stalled_since: float | None = None
        next_report = self._threshold
        while not self._stop.wait(self._interval):
            try:
                self._loop.call_soon_threadsafe(self._answer)
            except RuntimeError:  # the loop is closed: the core is gone, and so is the watch
                return
            silent_s = self._clock() - self._answered
            if silent_s >= next_report:
                if stalled_since is None:
                    stalled_since = self._answered
                    self.stalls += 1
                self._report(silent_s)
                next_report = silent_s + self._report_every
            elif stalled_since is not None and silent_s < self._threshold:
                log.warning("core.loop_resumed", stalled_s=round(self._answered - stalled_since, 1))
                stalled_since = None
                next_report = self._threshold

    def _report(self, silent_s: float) -> None:
        # sys._current_frames is the documented way to read another thread's stack.
        frame = sys._current_frames().get(self._loop_thread_id)  # noqa: SLF001
        log.warning("core.loop_stalled", for_s=round(silent_s, 1), stack=describe_stack(frame))
