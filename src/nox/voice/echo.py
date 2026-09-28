"""What the microphone hears of Nox's own voice, and what the pipeline does about it.

Nox has no acoustic echo cancellation. With speakers instead of headphones, the microphone picks up
every reply, and in continuous listening that echo looked like the user talking: it interrupted
the reply it came from (barge-in), and a reply that *quoted* the kill phrase could engage the kill
switch.

`EchoGuard` is the honest fallback, a half-duplex rule:

* While Nox speaks on a channel the user can hear, and for `ECHO_TAIL_S` after it stops, a speech
  segment is treated as echo unless it is clearly louder than the playback - `margin_db` above the
  level Nox is playing at, never below `BARGE_IN_FLOOR_DBFS`. Echo segments do not interrupt and
  are never transcribed. Push-to-talk is not affected: a held key is always the user.
* Independently of that setting, the guard remembers when Nox spoke a text that mentions the kill
  phrase. A kill phrase heard in an unforced segment overlapping that playback is Nox hearing
  itself and is ignored.

The levels compared are not calibrated to each other (the playback level is digital, the
microphone level depends on gain and distance), which is why the margin is a setting and why the
health reason says "half-duplex" rather than pretending to be echo cancellation.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import numpy as np

from nox.core.events import HealthStatus
from nox.util.aio import aclose
from nox.voice.base import Channel
from nox.voice.vad import frame_rms_db

#: How long after playback ended the room still carries Nox's voice (reverb, output latency).
ECHO_TAIL_S = 0.5
#: Assumed playback level until the first chunk of an utterance has been measured.
DEFAULT_PLAYBACK_DBFS = -20.0
#: A barge-in during playback must at least reach this level, however quiet the playback is.
BARGE_IN_FLOOR_DBFS = -35.0
#: Playback windows remembered for the kill-phrase self-trigger check.
_KILL_WINDOWS_KEPT = 8
_PCM16_FULL_SCALE = 32768.0
#: Channels whose audio reaches the room; the stream channel goes to a virtual cable only.
_AUDIBLE = (Channel.PRIVATE, Channel.BOTH)


def pcm16_dbfs(chunk: bytes) -> float:
    """RMS level of a PCM16 chunk in dBFS; -120 for an empty chunk."""
    usable = len(chunk) - len(chunk) % 2
    if usable <= 0:
        return -120.0
    samples = np.frombuffer(chunk[:usable], dtype=np.int16).astype(np.float32)
    return frame_rms_db(samples / _PCM16_FULL_SCALE)


@dataclass
class _Window:
    started: float
    ended: float = math.inf


class EchoGuard:
    """Half-duplex echo rule and kill-phrase self-trigger guard (see the module docstring)."""

    def __init__(
        self,
        *,
        half_duplex: bool = True,
        margin_db: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        tail_s: float = ECHO_TAIL_S,
    ) -> None:
        self.half_duplex = half_duplex
        self.margin_db = margin_db
        self.tail_s = tail_s
        self._clock = clock
        self._audible: _Window | None = None
        self._level_db = DEFAULT_PLAYBACK_DBFS
        self._measured = False
        self._kill_windows: deque[_Window] = deque(maxlen=_KILL_WINDOWS_KEPT)
        self._current_kill: _Window | None = None
        #: Segments not treated as the user because they were Nox's own voice.
        self.suppressed = 0

    # ---- playback side -------------------------------------------------------------------------

    def playback_started(self, *, channel: Channel, mentions_kill: bool) -> None:
        now = self._clock()
        self._level_db = DEFAULT_PLAYBACK_DBFS
        self._measured = False
        self._audible = _Window(started=now) if channel in _AUDIBLE else None
        self._current_kill = None
        if mentions_kill and channel in _AUDIBLE:
            self._current_kill = _Window(started=now)
            self._kill_windows.append(self._current_kill)

    def playback_level(self, chunk: bytes) -> None:
        level = pcm16_dbfs(chunk)
        if level > -120.0:
            # The loudest chunk so far: echo of a loud syllable must not slip under the bar.
            self._level_db = max(self._level_db, level) if self._measured else level
            self._measured = True

    def playback_ended(self) -> None:
        now = self._clock()
        if self._audible is not None:
            self._audible.ended = now
        if self._current_kill is not None:
            self._current_kill.ended = now
            self._current_kill = None

    async def metered(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Pass PCM chunks through unchanged while recording their level."""
        try:
            async for chunk in chunks:
                self.playback_level(chunk)
                yield chunk
        finally:
            await aclose(chunks)

    # ---- capture side --------------------------------------------------------------------------

    def in_echo_window(self) -> bool:
        """True while Nox is audible, or its voice may still be in the room."""
        if not self.half_duplex or self._audible is None:
            return False
        return self._clock() <= self._audible.ended + self.tail_s

    def threshold_dbfs(self) -> float:
        return max(self._level_db + self.margin_db, BARGE_IN_FLOOR_DBFS)

    def louder_than_playback(self, frame: np.ndarray) -> bool:
        return frame_rms_db(frame) >= self.threshold_dbfs()

    def heard_own_kill_phrase(self, started: float, ended: float) -> bool:
        """Did a segment spanning `started..ended` overlap Nox saying the kill phrase?"""
        return any(
            started <= window.ended + self.tail_s and ended >= window.started
            for window in self._kill_windows
        )

    def health(self) -> tuple[HealthStatus, str]:
        if self.half_duplex:
            return HealthStatus.LIMITED, (
                "half-duplex (no echo cancellation): while Nox speaks, only speech louder than "
                "the playback or push-to-talk interrupts it"
            )
        return HealthStatus.LIMITED, (
            "no echo cancellation: Nox's own voice from speakers can interrupt it - use "
            "headphones or turn on voice.stt.half_duplex"
        )
