"""A sensor whose poll fails keeps polling, and says that it is failing.

The sensor loops used to be `while True: await poll()`, so one exception from psutil or a Win32
call ended the task in silence while `sensors.status.read` kept serving the last sample as if it
were live.
"""

from __future__ import annotations

import asyncio

from nox.util.aio import poll_loop


async def test_a_failing_poll_does_not_end_the_loop() -> None:
    calls = 0
    errors: list[BaseException] = []
    third_call = asyncio.Event()

    async def poll() -> int:
        nonlocal calls
        calls += 1
        if calls >= 3:
            third_call.set()
        if calls == 1:
            raise RuntimeError("psutil is unhappy")
        return calls

    task = asyncio.create_task(poll_loop(poll, 0.0, name="test-sensor", on_error=errors.append))
    await asyncio.wait_for(third_call.wait(), timeout=5.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls >= 3  # it kept going after the failure
    assert [type(e).__name__ for e in errors] == ["RuntimeError"]


async def test_the_interval_may_depend_on_the_last_sample() -> None:
    intervals: list[float] = []
    twice = asyncio.Event()

    async def poll() -> int:
        return 7

    def interval(sample: int | None) -> float:
        intervals.append(0.0 if sample is None else float(sample))
        if len(intervals) >= 2:
            twice.set()
        return 0.0

    task = asyncio.create_task(poll_loop(poll, interval, name="adaptive"))
    await asyncio.wait_for(twice.wait(), timeout=5.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert intervals[:2] == [7.0, 7.0]
