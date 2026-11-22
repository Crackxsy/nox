"""When speech from the microphone counts as the user talking over Nox.

The voice detector opens a segment after about 90 ms of speech-like frames. That is quick, which is
right for listening - and wrong as the only condition for cutting off a spoken answer: a cough, a
door, a keyboard, the room's noise or the tail of Nox's own voice coming back through the speakers
all open segments. If every segment start ended the answer, one such sound would silence the whole
turn, and the user would never know why Nox stopped.

`BargeInGate` asks for more before it says yes: while a segment is open, it adds up the time of the
frames that are clearly loud (at or above `min_db`), and only when that adds up to `min_ms` is it
a barge-in. The two numbers are configurable (`voice.barge_in_min_ms`, `voice.barge_in_min_db`):

* 180 ms is a little more than the 90 ms the detector already needs: a click or a cough is over
  before it, a word such as "stop" is not (about 300 ms), so the answer is cut about a quarter of
  a second after the user starts to speak - which still feels immediate.
* -38 dBFS is a level a voice at a normal distance from a headset or desk microphone passes with
  room to spare (speech is roughly -30 to -20 dBFS there), while room noise (-60 to -45) and a
  quiet echo of Nox's own output (usually well below speech at the microphone) stay under it. A
  loud echo through speakers still passes; removing that echo is a separate job.

Push-to-talk never goes through this gate: pressing the key is the user saying so.
"""

from __future__ import annotations

import numpy as np

from nox.voice.vad import frame_rms_db

#: See the module docstring for why these two values.
DEFAULT_MIN_MS = 180
DEFAULT_MIN_DB = -38.0


class BargeInGate:
    """Adds up the loud speech of one open segment and says when it is long enough."""

    def __init__(
        self,
        *,
        min_ms: int = DEFAULT_MIN_MS,
        min_db: float = DEFAULT_MIN_DB,
        frame_ms: int = 30,
    ) -> None:
        if min_ms < 0 or frame_ms < 1:
            raise ValueError("min_ms must not be negative and frame_ms must be positive")
        self._min_ms = min_ms
        self._min_db = min_db
        self._frame_ms = frame_ms
        self._armed = False
        self._loud_ms = 0

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def loud_ms(self) -> int:
        return self._loud_ms

    def arm(self) -> None:
        """A segment opened while Nox was speaking: start adding up."""
        self._armed = True
        self._loud_ms = 0

    def disarm(self) -> None:
        """The segment ended, or nothing is being said any more: stop looking."""
        self._armed = False
        self._loud_ms = 0

    def observe(self, frame: np.ndarray) -> bool:
        """Count `frame`; True exactly once, on the frame that makes the speech long enough."""
        if not self._armed:
            return False
        if frame_rms_db(frame) >= self._min_db:
            self._loud_ms += self._frame_ms
        if self._loud_ms >= self._min_ms:
            self.disarm()
            return True
        return False
