"""`LoopWatch`: a blocked event loop is reported with the code that blocks it, and only then."""

from __future__ import annotations

import asyncio
import time

import structlog

from nox.core.loopwatch import LoopWatch, describe_stack


def _block_the_loop(seconds: float) -> None:
    time.sleep(seconds)  # synchronous on purpose: this is the stall being reported


async def test_a_blocked_loop_is_reported_with_its_stack_and_its_end() -> None:
    watch = LoopWatch(asyncio.get_running_loop(), threshold_s=0.3, interval_s=0.05)
    with structlog.testing.capture_logs() as captured:
        watch.start()
        try:
            await asyncio.sleep(0.2)
            _block_the_loop(0.8)
            await asyncio.sleep(0.3)  # the loop answers again; the watch notices
        finally:
            watch.stop()
    assert watch.stalls == 1
    stalled = [e for e in captured if e["event"] == "core.loop_stalled"]
    resumed = [e for e in captured if e["event"] == "core.loop_resumed"]
    assert stalled and resumed
    assert any("_block_the_loop" in line for line in stalled[0]["stack"])
    assert resumed[0]["stalled_s"] >= 0.6


async def test_a_loop_that_keeps_answering_is_never_reported() -> None:
    watch = LoopWatch(asyncio.get_running_loop(), threshold_s=0.3, interval_s=0.05)
    with structlog.testing.capture_logs() as captured:
        watch.start()
        try:
            for _ in range(10):
                _block_the_loop(0.05)  # short synchronous steps are a loop's normal life
                await asyncio.sleep(0.05)
        finally:
            watch.stop()
    assert watch.stalls == 0
    assert not [e for e in captured if e["event"].startswith("core.loop_")]


def test_the_stack_names_modules_and_functions_never_file_paths() -> None:
    import sys

    lines = describe_stack(sys._getframe())  # noqa: SLF001 - the current frame, for the test
    assert lines[-1].startswith(f"{__name__}:test_the_stack_names_modules")
    assert all("\\" not in line and "/" not in line for line in lines)
