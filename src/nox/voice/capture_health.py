"""Keeping the microphone alive: notice a dead capture path, restart it, and say so once.

The capture loop used to be a single task. When the input device went away (unplugged headset,
sleep/wake, a driver reset) that task either raised once and ended, or - more commonly - kept
waiting for frames that never came, and Nox stayed deaf without any sign of it. This module owns
what the pipeline does instead:

* `CaptureMonitor` is the state machine: `listening` -> `recovering` (a failure, retries running)
  -> `deaf` (several failures in a row) -> `listening` again once audio flows. It also decides when
  something is worth telling the user, which is exactly twice per episode: when it turns deaf and
  when it recovers from that, never on every retry.
* `CaptureSupervisor` runs the frame loop under supervision. It notices an ended or failed loop and
  a *stalled* one (capture is meant to be active, yet no frame arrived for a while), tears the
  input down, waits with a growing delay (capped) and opens it again. A changed set of input
  devices cuts the wait short, so plugging the headset back in is noticed within seconds.

Neither touches the privacy decision: whether capture is allowed at all stays with the pipeline's
gate, and a restart re-applies that gate rather than opening the device on its own. Time and
sleeping are injected, so the tests run on a fake clock without waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger

log = get_logger(__name__)

#: Wait before the n-th reopen attempt in a row; the last value repeats for as long as it takes.
RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
#: Consecutive failures after which the pipeline counts as deaf and tells the user.
DEAF_AFTER_FAILURES = 3
#: Capture is meant to be active but not a single frame arrived for this long: the device is gone.
STALL_AFTER_S = 3.0
#: How often the supervisor looks at the loop; also the granularity of every wait it does.
WATCH_TICK_S = 0.5
#: While waiting to retry, how often the list of input devices is compared with the last one.
DEVICE_POLL_S = 2.0


class CaptureState(StrEnum):
    LISTENING = "listening"
    RECOVERING = "recovering"
    DEAF = "deaf"


class CaptureNotice(StrEnum):
    """What is worth telling the user: each is raised once per episode."""

    DEAF = "deaf"
    RECOVERED = "recovered"


#: What Nox says about it, by language: a short sentence, not an error message.
_NOTICE_TEXTS: dict[CaptureNotice, dict[str, str]] = {
    CaptureNotice.DEAF: {
        "de": "Ich höre dich gerade nicht.",
        "en": "I can't hear you right now.",
    },
    CaptureNotice.RECOVERED: {
        "de": "Ich höre dich wieder.",
        "en": "I can hear you again.",
    },
}


def notice_text(notice: CaptureNotice, language: str) -> tuple[str, str]:
    """`(language, sentence)` for a notice; German unless the configured language is English."""
    texts = _NOTICE_TEXTS[notice]
    chosen = language if language in texts else "de"
    return chosen, texts[chosen]


class CaptureMonitor:
    """Counts consecutive capture failures and says when the user should be told."""

    def __init__(
        self,
        *,
        deaf_after: int = DEAF_AFTER_FAILURES,
        backoff_s: Sequence[float] = RETRY_BACKOFF_S,
    ) -> None:
        if deaf_after < 1 or not backoff_s:
            raise ValueError("deaf_after must be at least 1 and backoff_s must not be empty")
        self._deaf_after = deaf_after
        self._backoff_s = tuple(backoff_s)
        self.state = CaptureState.LISTENING
        self.reason = ""
        self.failures = 0
        self._announced_deaf = False

    @property
    def failing(self) -> bool:
        return self.state is not CaptureState.LISTENING

    def next_delay_s(self) -> float:
        """The wait before the next reopen attempt, given the failures so far."""
        index = min(max(self.failures, 1) - 1, len(self._backoff_s) - 1)
        return self._backoff_s[index]

    def failure(self, reason: str) -> CaptureNotice | None:
        """Record one failed attempt; returns `DEAF` the one time the user should hear about it."""
        self.failures += 1
        self.reason = reason
        if self.failures < self._deaf_after:
            self.state = CaptureState.RECOVERING
            return None
        self.state = CaptureState.DEAF
        if self._announced_deaf:
            return None
        self._announced_deaf = True
        return CaptureNotice.DEAF

    def frame(self) -> CaptureNotice | None:
        """A frame arrived while capture was meant to be active: audio flows again.

        Called for every frame, so the healthy case is a single comparison.
        """
        if self.state is CaptureState.LISTENING:
            return None
        notice = CaptureNotice.RECOVERED if self._announced_deaf else None
        self.reset()
        return notice

    def reset(self) -> None:
        self.state = CaptureState.LISTENING
        self.reason = ""
        self.failures = 0
        self._announced_deaf = False

    def health(self) -> tuple[HealthStatus, str]:
        """`unavailable` with the reason while capture is down, `available` otherwise."""
        if self.state is CaptureState.LISTENING:
            return HealthStatus.AVAILABLE, "capturing"
        retry = f"retrying in {self.next_delay_s():g} s"
        if self.state is CaptureState.DEAF:
            return HealthStatus.UNAVAILABLE, f"no microphone input, {retry}: {self.reason}"
        return HealthStatus.UNAVAILABLE, f"{self.reason} ({retry})"


class CaptureSupervisor:
    """Runs the pipeline's frame loop and brings it back when it dies or goes quiet."""

    def __init__(
        self,
        *,
        monitor: CaptureMonitor,
        listen: Callable[[], Awaitable[None]],
        is_active: Callable[[], bool],
        reopen: Callable[[], Awaitable[None]],
        announce: Callable[[CaptureNotice], None],
        stopped: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        input_devices: Callable[[], Sequence[str]] | None = None,
        tick_s: float = WATCH_TICK_S,
        stall_after_s: float = STALL_AFTER_S,
        device_poll_s: float = DEVICE_POLL_S,
    ) -> None:
        self._monitor = monitor
        self._listen = listen
        self._is_active = is_active
        self._reopen = reopen
        self._announce = announce
        self._stopped = stopped
        self._clock = clock
        self._sleep = sleep
        self._input_devices = input_devices
        self._tick_s = tick_s
        self._stall_after_s = stall_after_s
        self._device_poll_s = device_poll_s
        self._progress_at = clock()
        self._trouble = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ---- what the pipeline tells us ------------------------------------------------------------

    def note_frame(self) -> None:
        """One frame was processed while capture is active: progress, and proof of recovery."""
        self._progress_at = self._clock()
        notice = self._monitor.frame()
        if notice is not None:
            log.info("voice.capture_recovered")
            self._announce(notice)

    def report_open_failure(self, reason: str) -> None:
        """The device could not be opened when the gate asked for it (not found, in use, gone)."""
        self._failed(reason)
        self._trouble.set()

    # ---- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="voice-capture-supervisor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ---- the supervision loop ------------------------------------------------------------------

    def _failed(self, reason: str) -> None:
        notice = self._monitor.failure(reason)
        # Warn while the failure is new, then only every tenth time: a microphone that stays
        # gone must not fill the log with one line per retry.
        failures = self._monitor.failures
        level = log.warning if failures <= DEAF_AFTER_FAILURES or failures % 10 == 0 else log.debug
        level("voice.capture_failed", error=reason, failures=failures)
        if notice is not None:
            self._announce(notice)

    async def _guarded_listen(self) -> None:
        """One pass over the frames. A failure is recorded at once, so health shows it at once."""
        try:
            await self._listen()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported through health and retried, never hidden
            self._failed(f"microphone capture stopped: {type(exc).__name__}: {exc}")
            return
        if not self._stopped():
            self._failed("the capture device stopped delivering frames")

    async def _run(self) -> None:
        while not self._stopped():
            self._progress_at = self._clock()
            listener = asyncio.create_task(self._guarded_listen(), name="voice-listen")
            try:
                await self._watch(listener)
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            if self._stopped():
                return
            await self._recover()

    async def _watch(self, listener: asyncio.Task[None]) -> None:
        """Return when the loop ended, the device failed to open, or capture went quiet."""
        was_active = self._is_active()
        while True:
            if listener.done() or self._trouble.is_set() or self._stopped():
                return
            await self._sleep(self._tick_s)
            active = self._is_active()
            now = self._clock()
            if active and not was_active:
                self._progress_at = now  # the gate just opened: the silence before it is expected
            was_active = active
            if active and now - self._progress_at >= self._stall_after_s:
                self._failed(f"no audio from the microphone for {self._stall_after_s:g} s")
                return

    async def _recover(self) -> None:
        """Wait, reopen, repeat - until a reopen goes through or the pipeline stops."""
        self._trouble.clear()  # a failure reported from here on is seen by the next watch
        known = await self._device_names(())
        while not self._stopped():
            known = await self._wait_to_retry(self._monitor.next_delay_s(), known)
            if self._stopped():
                return
            try:
                await self._reopen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the next attempt is the handling
                self._failed(f"cannot reopen the microphone: {type(exc).__name__}: {exc}")
                continue
            log.info("voice.capture_reopened", failures=self._monitor.failures)
            return

    async def _wait_to_retry(self, delay_s: float, known: tuple[str, ...]) -> tuple[str, ...]:
        """Sleep up to `delay_s`, but give up waiting as soon as the input devices change."""
        waited = since_poll = 0.0
        while waited < delay_s and not self._stopped():
            step = min(self._tick_s, delay_s - waited)
            await self._sleep(step)
            waited += step
            since_poll += step
            if self._input_devices is None or since_poll < self._device_poll_s:
                continue
            since_poll = 0.0
            current = await self._device_names(known)
            if current != known:
                log.info("voice.capture_devices_changed")
                return current
        return known

    async def _device_names(self, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if self._input_devices is None:
            return fallback
        try:
            return tuple(await asyncio.to_thread(self._input_devices))
        except Exception as exc:  # noqa: BLE001 - device enumeration is only a hint to retry sooner
            log.debug("voice.capture_device_scan_failed", error=f"{type(exc).__name__}: {exc}")
            return fallback
