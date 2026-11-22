"""A microphone that goes away is noticed, restarted with a growing delay, and reported once.

No real audio and no real waiting: the input is a fake that can fail to open or stop delivering,
and time is a counter that advances only when the supervisor sleeps, so a half-minute backoff costs
nothing. The privacy side is pinned too - a restart must never open a device the gate keeps closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from nox.core.events import E, HealthStatus
from nox.voice import capture_health
from nox.voice.capture_health import (
    DEAF_AFTER_FAILURES,
    RETRY_BACKOFF_S,
    STALL_AFTER_S,
    CaptureMonitor,
    CaptureNotice,
    CaptureState,
    notice_text,
)
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeStt,
    FakeTts,
    settle,
    tone,
)


class FakeTime:
    """A clock that moves only when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class Rig:
    """A pipeline on fakes, with the clock wired into both the pipeline and the fake input."""

    def __init__(
        self,
        *,
        capture_allowed: Callable[[], bool] = lambda: True,
        input_devices: Callable[[], list[str]] | None = None,
        device_lock_wait: Callable[[float], Awaitable[None]] | None = None,
        **config: Any,
    ) -> None:
        self.time = FakeTime()
        self.audio_in = FakeAudioInput()
        self.audio_in.clock = self.time
        self.tts = FakeTts()
        self.events: list[tuple[str, dict[str, Any]]] = []

        async def emit(name: str, payload: dict[str, Any]) -> None:
            self.events.append((name, payload))

        self.pipeline = DefaultVoicePipeline(
            audio_in=self.audio_in,
            audio_out=FakeAudioOutput(),
            stt=FakeStt(),
            tts=self.tts,
            emit=emit,
            capture_allowed=capture_allowed,
            config=PipelineConfig(**config),
            clock=self.time,
            sleep=self.time.sleep,
            input_devices=input_devices,
            **({"device_lock_wait": device_lock_wait} if device_lock_wait else {}),
        )

    async def until(self, condition: Callable[[], bool], *, steps: int = 5000) -> None:
        for _ in range(steps):
            if condition():
                return
            await asyncio.sleep(0)
        raise AssertionError("the condition was never reached")

    async def run_for(self, seconds: float) -> None:
        """Let `seconds` of fake time pass, with the pipeline's tasks running."""
        until = self.time.now + seconds
        await self.until(lambda: self.time.now >= until)
        await settle()

    async def feed(self, frames: int) -> None:
        """Deliver `frames` frames of audio, a couple of loop passes apart."""
        for _ in range(frames):
            self.audio_in.push(tone(30)[:480])
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    def notices(self) -> list[str]:
        """The capture notices the worker sent to the core, in order."""
        return [p["notice"] for n, p in self.events if n == E.VOICE_CAPTURE_NOTICE]


@pytest.fixture(autouse=True)
def device_scan_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enumerate devices on the loop: a real thread would make the fake-time tests race a
    thread pool (slow on small machines) instead of running on the fake clock alone."""

    async def inline(function: Callable[[], Sequence[str]]) -> Sequence[str]:
        return function()

    monkeypatch.setattr(capture_health, "_in_thread", inline)


@pytest.fixture
def rig() -> Rig:
    return Rig()


# ---- the state machine ------------------------------------------------------------------------


def test_the_monitor_says_deaf_once_and_recovered_once() -> None:
    monitor = CaptureMonitor()
    notices = [monitor.failure("gone") for _ in range(DEAF_AFTER_FAILURES + 4)]
    assert notices.count(CaptureNotice.DEAF) == 1
    assert notices.index(CaptureNotice.DEAF) == DEAF_AFTER_FAILURES - 1  # on the third failure
    assert monitor.state is CaptureState.DEAF

    assert monitor.frame() is CaptureNotice.RECOVERED
    assert monitor.frame() is None
    assert monitor.state is CaptureState.LISTENING and monitor.failures == 0


def test_a_hiccup_that_recovers_before_the_deaf_threshold_is_never_announced() -> None:
    monitor = CaptureMonitor()
    for _ in range(DEAF_AFTER_FAILURES - 1):
        assert monitor.failure("reset") is None
    assert monitor.state is CaptureState.RECOVERING
    assert monitor.frame() is None  # recovered quietly
    assert monitor.state is CaptureState.LISTENING


def test_a_second_episode_is_announced_again() -> None:
    monitor = CaptureMonitor()
    for _ in range(DEAF_AFTER_FAILURES):
        monitor.failure("gone")
    monitor.frame()
    notices = [monitor.failure("gone again") for _ in range(DEAF_AFTER_FAILURES)]
    assert notices[-1] is CaptureNotice.DEAF


def test_the_retry_delay_follows_the_schedule_and_stops_growing() -> None:
    monitor = CaptureMonitor()
    delays = []
    for _ in range(len(RETRY_BACKOFF_S) + 3):
        monitor.failure("gone")
        delays.append(monitor.next_delay_s())
    assert tuple(delays[: len(RETRY_BACKOFF_S)]) == RETRY_BACKOFF_S == (1.0, 2.0, 5.0, 10.0, 30.0)
    assert delays[len(RETRY_BACKOFF_S) :] == [30.0, 30.0, 30.0]


@pytest.mark.parametrize(
    ("failures", "status"),
    [(0, HealthStatus.AVAILABLE), (1, HealthStatus.UNAVAILABLE), (3, HealthStatus.UNAVAILABLE)],
)
def test_the_monitor_health_follows_its_state(failures: int, status: HealthStatus) -> None:
    monitor = CaptureMonitor()
    for _ in range(failures):
        monitor.failure("the headset was unplugged")
    assert monitor.health()[0] is status
    if failures:
        assert "the headset was unplugged" in monitor.health()[1]


def test_the_notice_is_german_unless_english_is_configured() -> None:
    assert notice_text(CaptureNotice.DEAF, "auto") == ("de", "Ich höre dich gerade nicht.")
    assert notice_text(CaptureNotice.DEAF, "en") == ("en", "I can't hear you right now.")
    assert notice_text(CaptureNotice.RECOVERED, "de") == ("de", "Ich höre dich wieder.")
    assert notice_text(CaptureNotice.RECOVERED, "fr")[0] == "de"


# ---- restarting -------------------------------------------------------------------------------


async def test_a_capture_loop_that_ends_is_started_again_and_listens(rig: Rig) -> None:
    await rig.pipeline.start()
    assert rig.audio_in.starts == 1

    rig.audio_in.queue.put_nowait(None)  # the device stops delivering frames
    await rig.until(lambda: rig.audio_in.starts == 2)
    await rig.feed(5)  # audio flows again

    assert rig.pipeline.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    assert rig.notices() == []  # a single hiccup is nothing to talk about
    await rig.pipeline.stop()


async def test_a_loop_that_raises_is_restarted_and_health_names_the_error(rig: Rig) -> None:
    await rig.pipeline.start()
    rig.audio_in.fail_with = RuntimeError("PortAudio device lost")
    rig.audio_in.push(tone(30)[:480])
    await rig.until(lambda: rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE)
    assert "PortAudio device lost" in rig.pipeline.capture_health()[1]

    rig.audio_in.fail_with = None
    await rig.until(lambda: rig.audio_in.starts == 2)
    await rig.feed(5)
    assert rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE
    await rig.pipeline.stop()


async def test_restart_attempts_wait_one_two_five_ten_then_thirty_seconds(rig: Rig) -> None:
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()  # opening fails right away: no exception for the caller
    await rig.until(lambda: len(rig.audio_in.start_times) >= 8)

    times = rig.audio_in.start_times
    gaps = [round(b - a, 1) for a, b in zip(times, times[1:], strict=False)]
    assert gaps[:5] == [1.0, 2.0, 5.0, 10.0, 30.0]
    assert gaps[5:] == [30.0] * len(gaps[5:])  # capped, and it keeps trying
    await rig.pipeline.stop()


async def test_a_microphone_that_stays_gone_is_announced_once_and_not_on_every_retry(
    rig: Rig,
) -> None:
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()

    await rig.run_for(sum(RETRY_BACKOFF_S) + 5 * 30.0)  # many more attempts than the threshold

    assert len(rig.audio_in.start_times) > DEAF_AFTER_FAILURES + 3
    assert rig.notices() == ["deaf"]
    status, reason = rig.pipeline.capture_health()
    assert status is HealthStatus.UNAVAILABLE
    assert "no input device" in reason
    await rig.pipeline.stop()


async def test_the_worker_does_not_speak_the_notice_itself(rig: Rig) -> None:
    """An unprompted sentence is the core's to allow (quiet hours, privacy zones, ...)."""
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: bool(rig.notices()))

    assert rig.tts.requests == []
    assert rig.pipeline._say_lock.locked() is False  # noqa: SLF001 - nothing was queued to play
    await rig.pipeline.stop()


async def test_recovery_after_a_long_outage_is_announced_once(rig: Rig) -> None:
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.run_for(sum(RETRY_BACKOFF_S))

    rig.audio_in.open_error = None  # the headset is plugged back in
    await rig.until(lambda: rig.audio_in.enabled)
    await rig.feed(120)  # audio keeps flowing, for a minute of fake time and more

    assert rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE
    assert rig.notices() == ["deaf", "recovered"]
    await rig.pipeline.stop()


async def test_a_quiet_microphone_is_a_failure_while_capture_is_meant_to_be_active(
    rig: Rig,
) -> None:
    await rig.pipeline.start()
    assert rig.audio_in.starts == 1

    await rig.run_for(STALL_AFTER_S + 2)  # not a single frame arrives

    assert rig.audio_in.starts >= 2  # the supervisor tore the input down and opened it again
    await rig.pipeline.stop()


async def test_frames_keep_the_stall_watchdog_quiet(rig: Rig) -> None:
    await rig.pipeline.start()
    for _ in range(200):
        rig.audio_in.push(tone(30)[:480])
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert rig.time.now > 10 * STALL_AFTER_S  # plenty of fake time passed, with frames throughout
    assert rig.audio_in.starts == 1
    assert rig.pipeline.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    await rig.pipeline.stop()


async def test_a_closed_gate_is_silence_not_a_stall() -> None:
    """Muted, push-to-talk waiting, privacy off: no frames are expected, so none are missed."""
    rig = Rig(listening_mode="ptt_only")
    await rig.pipeline.start()

    await rig.run_for(10 * STALL_AFTER_S)

    assert rig.audio_in.starts == 1
    assert rig.audio_in.opens == 0
    assert rig.notices() == []
    assert rig.pipeline.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    await rig.pipeline.stop()


# ---- the gate stays in charge -----------------------------------------------------------------


async def test_a_restart_never_opens_a_device_the_gate_keeps_closed() -> None:
    allowed = {"mic": True}
    rig = Rig(capture_allowed=lambda: allowed["mic"])
    await rig.pipeline.start()
    allowed["mic"] = False  # privacy: the microphone is switched off
    await rig.pipeline.refresh_gate()
    assert rig.audio_in.enabled is False
    opens_before = rig.audio_in.opens

    rig.audio_in.queue.put_nowait(None)  # the loop dies while the gate is closed
    await rig.until(lambda: rig.audio_in.starts == 2)
    await rig.run_for(30)

    assert rig.audio_in.enabled is False
    assert rig.audio_in.opens == opens_before  # not opened again by the restart
    await rig.pipeline.stop()


async def test_a_failure_to_open_on_push_to_talk_is_reported_not_raised() -> None:
    rig = Rig(listening_mode="ptt_only")
    await rig.pipeline.start()
    rig.audio_in.open_error = OSError("no input device")

    await rig.pipeline.push_to_talk(True)  # must not raise into the IPC handler

    status, reason = rig.pipeline.capture_health()
    assert status is HealthStatus.UNAVAILABLE and "no input device" in reason
    rig.audio_in.open_error = None
    await rig.until(lambda: rig.audio_in.enabled)  # the supervisor re-applied the held key
    await rig.pipeline.push_to_talk(False)
    assert rig.audio_in.enabled is False
    await rig.pipeline.stop()


async def test_nothing_is_said_once_the_kill_phrase_is_latched_or_the_pipeline_stopped() -> None:
    rig = Rig()
    await rig.pipeline.start()
    pipeline = rig.pipeline

    pipeline._kill_latched = True  # noqa: SLF001 - what a heard kill phrase leaves behind
    pipeline._announce_capture(CaptureNotice.DEAF)  # noqa: SLF001
    await settle()
    assert rig.notices() == []

    pipeline._kill_latched = False  # noqa: SLF001
    pipeline._announce_capture(CaptureNotice.DEAF)  # noqa: SLF001
    await rig.until(lambda: bool(rig.notices()))
    assert rig.notices() == ["deaf"]  # so the silence above was the guard

    await pipeline.stop()
    pipeline._announce_capture(CaptureNotice.RECOVERED)  # noqa: SLF001
    await settle()
    assert rig.notices() == ["deaf"]


# ---- plugging a device in ---------------------------------------------------------------------


async def test_a_new_input_device_ends_the_wait_instead_of_sitting_out_the_backoff() -> None:
    devices = ["Headset (built-in)"]
    rig = Rig(input_devices=lambda: list(devices))
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: len(rig.audio_in.start_times) >= 7)  # deep into the 30 s steps
    last = rig.audio_in.start_times[-1]

    devices.append("USB headset")  # plugged in right after an attempt
    await rig.until(lambda: len(rig.audio_in.start_times) >= 8)

    assert rig.audio_in.start_times[-1] - last < 5.0  # a poll interval, not 30 s
    await rig.pipeline.stop()


async def test_the_devices_staying_the_same_does_not_shorten_the_wait() -> None:
    rig = Rig(input_devices=lambda: ["Headset (built-in)"])
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: len(rig.audio_in.start_times) >= 8)

    gaps = [
        b - a for a, b in zip(rig.audio_in.start_times, rig.audio_in.start_times[1:], strict=False)
    ]
    assert gaps[-1] == 30.0
    await rig.pipeline.stop()


async def test_a_device_list_that_cannot_be_read_is_not_an_error() -> None:
    def broken() -> list[str]:
        raise OSError("PortAudio is not initialised")

    rig = Rig(input_devices=broken)
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: len(rig.audio_in.start_times) >= 4)
    assert rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE
    await rig.pipeline.stop()


# ---- a closed gate ends the failure ------------------------------------------------------------


async def test_a_failure_does_not_outlive_the_push_to_talk_that_provoked_it() -> None:
    """Push-to-talk with a broken microphone: once the key is up nobody asks it to work."""
    rig = Rig(listening_mode="ptt_only")
    await rig.pipeline.start()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.push_to_talk(True)
    assert rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE

    await rig.pipeline.push_to_talk(False)
    await rig.until(lambda: rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE)
    starts = rig.audio_in.starts
    await rig.run_for(120)

    assert rig.pipeline.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    assert rig.audio_in.starts == starts  # and nothing keeps retrying
    assert rig.notices() == []
    await rig.pipeline.stop()


async def test_muting_a_broken_microphone_clears_the_failure_and_unmuting_reports_it_afresh() -> (
    None
):
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    assert rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE

    await rig.pipeline.set_muted(True)
    await rig.until(lambda: rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE)
    starts = rig.audio_in.starts
    await rig.run_for(120)
    assert rig.audio_in.starts == starts

    await rig.pipeline.set_muted(False)  # the device is still broken
    assert rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE
    assert "no input device" in rig.pipeline.capture_health()[1]
    await rig.pipeline.stop()


async def test_a_deaf_episode_that_ends_by_muting_is_not_announced_as_recovered() -> None:
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: rig.notices() == ["deaf"])

    await rig.pipeline.set_muted(True)
    await rig.run_for(60)

    assert rig.notices() == ["deaf"]
    await rig.pipeline.stop()


# ---- a microphone that keeps dropping out ------------------------------------------------------


class Ticker:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_relapses_shortly_after_a_recovery_continue_the_count_and_are_announced_once() -> None:
    clock = Ticker()
    monitor = CaptureMonitor(clock=clock)
    notices = []
    for relapse in range(5):
        notices.append(monitor.failure("dropped out"))
        assert monitor.next_delay_s() == RETRY_BACKOFF_S[min(relapse, 4)]  # keeps growing
        clock.now += 5.0
        assert monitor.frame() is None  # back for a few seconds each time
        clock.now += 5.0

    assert notices == [None, None, CaptureNotice.UNSTABLE, None, None]


def test_a_microphone_that_was_stable_for_a_minute_starts_over() -> None:
    clock = Ticker()
    monitor = CaptureMonitor(clock=clock)
    for _ in range(3):
        monitor.failure("dropped out")
        monitor.frame()
    clock.now += 61.0

    assert monitor.failure("dropped out") is None
    assert monitor.failures == 1 and monitor.next_delay_s() == 1.0


def test_a_relapse_that_never_recovers_turns_deaf_after_all() -> None:
    clock = Ticker()
    monitor = CaptureMonitor(clock=clock)
    for _ in range(3):
        monitor.failure("dropped out")
        monitor.frame()
    notices = [monitor.failure("gone") for _ in range(4)]

    assert CaptureNotice.DEAF in notices
    assert monitor.state is CaptureState.DEAF


async def test_a_flapping_microphone_backs_off_and_says_so_once(rig: Rig) -> None:
    await rig.pipeline.start()
    for cycle in range(5):
        rig.audio_in.queue.put_nowait(None)  # drops out ...
        await rig.until(lambda cycle=cycle: rig.audio_in.starts == cycle + 2)
        await rig.feed(3)  # ... and is back

    times = rig.audio_in.start_times
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert gaps[2] > gaps[1] > gaps[0]  # each relapse waits longer than the one before
    assert rig.notices() == ["unstable"]
    await rig.pipeline.stop()


# ---- stopping ---------------------------------------------------------------------------------


async def test_stop_waits_for_a_device_that_is_being_opened_and_closes_it(rig: Rig) -> None:
    await rig.pipeline.start()
    await rig.pipeline._device_lock.acquire()  # noqa: SLF001 - a reopen is opening the device
    stopping = asyncio.create_task(rig.pipeline.stop())
    await settle()
    assert not stopping.done()  # not closed from under the reopen

    rig.pipeline._device_lock.release()  # noqa: SLF001
    await stopping

    assert rig.audio_in.enabled is False and rig.audio_in.stopped


# ---- stopping closes the device under the lock, and never hangs on it --------------------------


async def test_stop_closes_the_input_while_holding_the_device_lock(rig: Rig) -> None:
    """Both closing calls happen under the lock: a reopen that is just opening the device is
    waited for, so no stream is left open behind a stopped pipeline."""
    await rig.pipeline.start()
    seen: list[tuple[str, bool]] = []
    set_enabled, stop = rig.audio_in.set_enabled, rig.audio_in.stop

    async def spy_set_enabled(value: bool) -> None:
        seen.append((f"set_enabled({value})", rig.pipeline._device_lock.locked()))  # noqa: SLF001
        await set_enabled(value)

    async def spy_stop() -> None:
        seen.append(("stop", rig.pipeline._device_lock.locked()))  # noqa: SLF001
        await stop()

    rig.audio_in.set_enabled = spy_set_enabled  # type: ignore[method-assign]
    rig.audio_in.stop = spy_stop  # type: ignore[method-assign]

    await rig.pipeline.stop()

    assert seen == [("set_enabled(False)", True), ("stop", True)]
    assert rig.pipeline._device_lock.locked() is False  # noqa: SLF001 - and it was given back


async def test_stop_does_not_wait_for_ever_on_a_device_that_hangs_while_opening() -> None:
    """The kill switch calls `stop`. A reopen stuck inside the device driver holds the lock for
    as long as the driver takes - the kill switch must not take as long."""
    give_up = asyncio.Event()

    async def wait(_seconds: float) -> None:
        await give_up.wait()

    rig = Rig(device_lock_wait=wait)
    await rig.pipeline.start()
    await rig.pipeline._device_lock.acquire()  # noqa: SLF001 - the stuck reopen
    stopping = asyncio.create_task(rig.pipeline.stop())
    await settle()
    assert not stopping.done()  # it does wait for a while ...

    give_up.set()  # ... and then stops waiting
    await asyncio.wait_for(stopping, 1)

    assert rig.audio_in.enabled is False and rig.audio_in.stopped
    assert rig.pipeline._device_lock.locked() is True  # noqa: SLF001 - not its lock to release
    rig.pipeline._device_lock.release()  # noqa: SLF001


async def test_a_holder_that_lets_go_in_time_is_waited_for_in_full() -> None:
    gone = asyncio.Event()

    async def wait(_seconds: float) -> None:
        await gone.wait()  # never ends in this test

    rig = Rig(device_lock_wait=wait)
    await rig.pipeline.start()
    await rig.pipeline._device_lock.acquire()  # noqa: SLF001
    stopping = asyncio.create_task(rig.pipeline.stop())
    await settle()
    rig.pipeline._device_lock.release()  # noqa: SLF001
    await asyncio.wait_for(stopping, 1)

    assert rig.audio_in.stopped and rig.pipeline._device_lock.locked() is False  # noqa: SLF001


async def test_a_reopen_that_finishes_after_stop_leaves_the_input_closed(rig: Rig) -> None:
    await rig.pipeline.start()
    await rig.pipeline.stop()

    await rig.pipeline._reopen_capture()  # noqa: SLF001 - the slow reopen, finishing late

    assert rig.audio_in.enabled is False


# ---- a failure is neither lost nor counted twice -----------------------------------------------


async def test_a_failure_noted_while_waiting_is_not_held_against_the_device_that_came_back() -> (
    None
):
    """It was reported before the reopen and is already part of the retry delay: once the reopen
    works, it must not send the supervisor round again."""
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: rig.time.now >= 0.5)  # inside the first retry delay

    rig.pipeline._supervisor.report_open_failure("the device flickered")  # noqa: SLF001
    rig.audio_in.open_error = None
    await rig.until(lambda: rig.audio_in.starts == 2)
    await rig.feed(120)  # audio keeps flowing, for a minute of fake time and more

    assert rig.time.now > 30
    assert rig.audio_in.starts == 2  # one restart; not a second one for the same trouble
    assert rig.pipeline.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    await rig.pipeline.stop()


async def test_a_failure_during_the_reopen_itself_is_not_lost() -> None:
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: rig.time.now >= 0.5)
    rig.audio_in.open_error = None
    rig.audio_in.on_start = lambda: rig.pipeline._supervisor.report_open_failure(  # noqa: SLF001
        "the device dropped while it was being opened"
    )

    await rig.until(lambda: rig.audio_in.starts == 2)
    rig.audio_in.on_start = None
    await rig.until(lambda: rig.audio_in.starts >= 3 or rig.time.now > 20)

    # Picked up at once (the second retry delay, two seconds), not left to the three-second
    # stall watchdog and the longer wait that follows it.
    assert rig.audio_in.starts == 3 and rig.time.now <= 4.0
    await rig.pipeline.stop()


# ---- "I can hear you again" after a mute --------------------------------------------------------


async def test_the_user_who_was_told_nox_is_deaf_is_told_when_it_hears_again_after_a_mute() -> None:
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: rig.notices() == ["deaf"])
    await rig.pipeline.set_muted(True)  # the user mutes: the episode is closed on purpose
    await rig.until(lambda: rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE)
    assert rig.notices() == ["deaf"]  # nothing flows while muted: nothing to announce

    rig.audio_in.open_error = None  # the headset was plugged in meanwhile
    await rig.pipeline.set_muted(False)
    await rig.feed(10)

    assert rig.notices() == ["deaf", "recovered"]
    await rig.feed(30)
    assert rig.notices() == ["deaf", "recovered"]  # once
    await rig.pipeline.stop()


async def test_a_mute_that_hides_a_microphone_that_is_still_broken_does_not_announce_again() -> (
    None
):
    rig = Rig()
    rig.audio_in.open_error = OSError("no input device")
    await rig.pipeline.start()
    await rig.until(lambda: rig.notices() == ["deaf"])
    await rig.pipeline.set_muted(True)
    await rig.until(lambda: rig.pipeline.capture_health()[0] is HealthStatus.AVAILABLE)

    await rig.pipeline.set_muted(False)  # still no microphone
    await rig.run_for(sum(RETRY_BACKOFF_S))

    assert rig.notices() == ["deaf"]  # the user knows; it is not said again
    assert rig.pipeline.capture_health()[0] is HealthStatus.UNAVAILABLE
    await rig.pipeline.stop()


def test_the_monitor_owes_the_recovery_notice_across_a_settle_but_not_across_a_reset() -> None:
    monitor = CaptureMonitor()
    for _ in range(DEAF_AFTER_FAILURES):
        monitor.failure("gone")

    monitor.settle()
    assert monitor.state is CaptureState.LISTENING and monitor.failures == 0
    assert monitor.frame() is CaptureNotice.RECOVERED
    assert monitor.frame() is None

    for _ in range(DEAF_AFTER_FAILURES):
        monitor.failure("gone")
    monitor.reset()  # a fresh start forgets what was said
    assert monitor.frame() is None


def test_a_settle_without_a_deaf_notice_owes_nothing() -> None:
    monitor = CaptureMonitor()
    monitor.failure("one hiccup")

    monitor.settle()

    assert monitor.frame() is None
