"""Speech running ahead of its turn: the next sentence is synthesised while this one is heard.

The core hands sentences to the voice worker one request each, and they are spoken in order. Left
to itself every sentence would only start to be synthesised once the one before it had been heard to
the end, so each sentence boundary cost the whole time-to-first-audio of the engine as dead air.
`PrefetchedSpeech` is the other way round: the synthesis of a sentence that is still waiting for its
turn is started early and collected into a buffer that has a size, so a fast engine cannot run
away from a slow device and a sentence that is never played does not grow without limit. When
its turn comes the sentence plays from the buffer and then from whatever the engine is still
producing, as one stream.

Three rules keep this safe on a machine that is busy with something else:

* the look-ahead has a size (`sentences`, at most two) and a buffer ceiling per sentence;
* it only starts while the machine has room (`ResourceGate`): when a game keeps the processor
  busy, a sentence is synthesised when its turn comes, as before - slower, never in the way;
* cancelling it is immediate and complete: the synthesis task is cancelled, the engine's stream is
  closed and the buffered audio is thrown away, so an interrupted turn leaves nothing behind.

Whatever the engine raises is kept and re-raised to the listener *after* the audio that was
produced before it, so a sentence fails exactly where it would have failed without the look-ahead.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Callable

from nox.util.aio import aclose
from nox.voice._logging import get_logger

log = get_logger(__name__)

#: PCM16 mono: bytes per second of audio per Hz of sample rate.
BYTES_PER_SAMPLE = 2
#: How many sentences may be synthesised ahead of the one that is playing. Two is the ceiling:
#: beyond it the work is thrown away by the first interruption and the machine pays for nothing.
MAX_LOOKAHEAD = 2


class PrefetchedSpeech:
    """The synthesis of one sentence, started early and read as one stream.

    `source_factory` creates the engine's audio stream; it is called when the synthesis starts, so
    a sentence that is dropped before it begins never touches the engine.
    """

    def __init__(
        self,
        source_factory: Callable[[], AsyncIterator[bytes]],
        *,
        max_bytes: int,
        name: str = "",
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._factory = source_factory
        self._max_bytes = max_bytes
        self._name = name
        self._chunks: deque[bytes] = deque()
        self._bytes = 0
        self._finished = False
        self._cancelled = False
        self._error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None
        self._source: AsyncIterator[bytes] | None = None
        #: Set when there is something to read, or nothing more will come.
        self._readable = asyncio.Event()
        #: Set while the buffer has room for the producer's next chunk.
        self._room = asyncio.Event()
        self._room.set()
        #: Whether the audio was ready, at least partly, before anyone asked for it.
        self.was_ahead = False

    # ---- state -------------------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._task is not None

    @property
    def finished(self) -> bool:
        """The engine has delivered its last chunk (or failed); the buffer may still hold audio."""
        return self._finished

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    # ---- producing ---------------------------------------------------------------------------

    def start(self) -> None:
        """Begin synthesising. Does nothing twice, and nothing once cancelled."""
        if self._task is not None or self._cancelled:
            return
        self._task = asyncio.create_task(self._produce(), name=f"voice-prefetch-{self._name}")

    async def _produce(self) -> None:
        source = self._source = self._factory()
        try:
            async for chunk in source:
                while self._bytes >= self._max_bytes and not self._cancelled:
                    self._room.clear()
                    await self._room.wait()
                if self._cancelled:
                    return
                self._chunks.append(chunk)
                self._bytes += len(chunk)
                self._readable.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - handed to the listener, after the audio before it
            self._error = exc
        finally:
            self._finished = True
            self._readable.set()
            await aclose(source)

    # ---- listening ---------------------------------------------------------------------------

    async def chunks(self) -> AsyncIterator[bytes]:
        """The sentence's audio in order. Starts the synthesis when it has not started yet; ends
        quietly when cancelled; raises what the engine raised once the earlier audio was read."""
        if self._task is None:
            self.start()
        else:
            self.was_ahead = self._bytes > 0 or self._finished
        while True:
            if self._chunks:
                chunk = self._chunks.popleft()
                self._bytes -= len(chunk)
                self._room.set()
                yield chunk
                continue
            if self._finished or self._cancelled:
                break
            self._readable.clear()
            await self._readable.wait()
        if self._error is not None and not self._cancelled:
            raise self._error

    # ---- cancelling --------------------------------------------------------------------------

    def cancel_nowait(self) -> None:
        """Stop synthesising and drop the buffered audio, without waiting for the task to end.

        Safe to call from anywhere on the event loop and as often as wanted; this is what an
        interruption uses, so nothing in it awaits.
        """
        if self._cancelled:
            return
        self._cancelled = True
        self._chunks.clear()
        self._bytes = 0
        self._room.set()
        self._readable.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def cancel(self) -> None:
        """`cancel_nowait`, then wait until the synthesis task has really ended and its stream is
        closed. The task may have been cancelled on its way in, before it ever ran."""
        self.cancel_nowait()
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(task, return_exceptions=True)
        if self._source is not None:
            await aclose(self._source)


class ResourceGate:
    """Whether the machine has room for work nobody is waiting for yet.

    The look-ahead is a luxury: it spends processor time now to save a pause later. While a game or
    anything else keeps the processor busy that trade is wrong, so `free()` says no above the
    ceiling and the sentence is synthesised only when its turn comes. `sample` returns the system's
    processor load as a fraction, or None when it cannot be measured - and then the look-ahead is
    allowed, because refusing on a missing number would switch the feature off for no reason.
    """

    def __init__(self, ceiling: float, sample: Callable[[], float | None]) -> None:
        if not 0.0 < ceiling <= 1.0:
            raise ValueError("ceiling must be in (0, 1]")
        self._ceiling = ceiling
        self._sample = sample

    @property
    def ceiling(self) -> float:
        return self._ceiling

    def free(self) -> bool:
        try:
            load = self._sample()
        except Exception as exc:  # noqa: BLE001 - a broken probe must not silence the look-ahead
            log.debug("voice.prefetch_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return True
        return load is None or load < self._ceiling


def system_cpu_load() -> float | None:
    """The whole machine's processor load since the last call, as a fraction; None without psutil.

    Not blocking (`interval=None`): it compares with the previous call, so the first answer is a
    meaningless zero and later ones describe the time between two sentences - which is exactly
    the window the look-ahead decision is about.
    """
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None)) / 100.0
    except Exception:  # noqa: BLE001 - load is a hint, never a reason to fail
        return None
