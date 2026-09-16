"""Energy/zero-crossing voice activity detection and utterance segmentation in numpy (FR-5.1,
ADR-009).

No third-party VAD (webrtcvad has no 3.13 wheels); the detector tracks an adaptive noise floor and
flags a 30 ms frame as speech when its RMS level rises clearly above that floor and the
zero-crossing
rate is in the range of voiced/fricative speech rather than broadband hiss. The segmenter turns the
per-frame decisions into utterances with pre-roll, hangover and a hard maximum length. It also
supports a forced (push-to-talk) segment that ignores the VAD entirely.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

_EPS = 1e-9


def frame_rms_db(frame: np.ndarray) -> float:
    """RMS level of a float32 frame in dBFS (0 dBFS = full scale sine RMS 1.0)."""
    if frame.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
    return float(20.0 * np.log10(rms + _EPS))


def frame_zcr(frame: np.ndarray) -> float:
    """Zero-crossing rate: fraction of sample pairs whose sign changes (0..1)."""
    if frame.size < 2:
        return 0.0
    signs = np.signbit(frame)
    return float(np.count_nonzero(signs[1:] != signs[:-1])) / float(frame.size - 1)


@dataclass
class EnergyVad:
    """Per-frame speech decision with an adaptive noise floor.

    - `absolute_floor_db`: below this level nothing is speech (silence/mute).
    - `margin_db`: a frame must exceed the tracked noise floor by this much.
    - `zcr_min`/`zcr_max`: zero-crossing band; pure DC/hum sits below, hiss/clicks above.
    - The noise floor follows quiet frames quickly and loud frames very slowly, so it does not
      learn the user's speech as noise during a long utterance.
    """

    absolute_floor_db: float = -50.0
    margin_db: float = 8.0
    zcr_min: float = 0.005
    zcr_max: float = 0.45
    noise_floor_db: float = -60.0
    floor_attack: float = 0.05  # how fast the floor rises towards a louder quiet level
    floor_release: float = 0.2  # how fast the floor drops towards a quieter level
    max_floor_db: float = -25.0  # the floor never rises above this; louder is always "speech"

    def reset(self) -> None:
        self.noise_floor_db = -60.0

    def level(self, frame: np.ndarray) -> tuple[float, float]:
        return frame_rms_db(frame), frame_zcr(frame)

    def is_speech(self, frame: np.ndarray) -> bool:
        rms_db, zcr = self.level(frame)
        speech = (
            rms_db > self.absolute_floor_db
            and rms_db > self.noise_floor_db + self.margin_db
            and self.zcr_min <= zcr <= self.zcr_max
        )
        if not speech:
            # Update the floor only from non-speech frames (asymmetric smoothing).
            if rms_db < self.noise_floor_db:
                self.noise_floor_db += (rms_db - self.noise_floor_db) * self.floor_release
            else:
                self.noise_floor_db += (rms_db - self.noise_floor_db) * self.floor_attack
            self.noise_floor_db = min(self.noise_floor_db, self.max_floor_db)
        return speech


class SegmentKind(StrEnum):
    START = "start"
    END = "end"
    ABORT = "abort"  # segment discarded (too short)


@dataclass
class SegmentEvent:
    kind: SegmentKind
    audio: np.ndarray | None = None
    duration_ms: int = 0
    forced: bool = False


@dataclass
class Segmenter:
    """Turns frame decisions into utterances.

    - Speech starts after `start_frames` consecutive speech frames; `pre_roll_ms` of earlier audio
      is
      included so the first phoneme is not clipped.
    - Speech ends after `end_silence_ms` without speech, or when `max_segment_ms` is reached.
    - Segments shorter than `min_segment_ms` are dropped (ABORT) to avoid transcribing clicks.
    - `force_start()`/`force_end()` implement push-to-talk: every frame is collected until release.
    """

    vad: EnergyVad = field(default_factory=EnergyVad)
    sample_rate: int = 16000
    frame_ms: int = 30
    start_frames: int = 3
    end_silence_ms: int = 600
    pre_roll_ms: int = 300
    min_segment_ms: int = 250
    max_segment_ms: int = 15000

    _pre_roll: deque[np.ndarray] = field(default_factory=deque, init=False)
    _speech_run: int = field(default=0, init=False)
    _silence_run: int = field(default=0, init=False)
    _buffer: list[np.ndarray] = field(default_factory=list, init=False)
    _active: bool = field(default=False, init=False)
    _forced: bool = field(default=False, init=False)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def forced(self) -> bool:
        return self._forced

    def reset(self) -> None:
        self._pre_roll.clear()
        self._speech_run = 0
        self._silence_run = 0
        self._buffer = []
        self._active = False
        self._forced = False

    def _ms(self, frames: int) -> int:
        return frames * self.frame_ms

    def force_start(self) -> SegmentEvent | None:
        """Push-to-talk pressed: open a segment now (keeps a running VAD segment)."""
        if self._active:
            self._forced = True
            return None
        self._active = True
        self._forced = True
        self._buffer = list(self._pre_roll)
        self._silence_run = 0
        return SegmentEvent(SegmentKind.START, forced=True)

    def force_end(self) -> SegmentEvent | None:
        """Push-to-talk released: close the segment regardless of the VAD."""
        if not self._active:
            self._forced = False
            return None
        return self._finish(forced=True)

    def push(self, frame: np.ndarray) -> SegmentEvent | None:
        speech = self.vad.is_speech(frame)
        if self._active:
            self._buffer.append(frame)
            if speech:
                self._speech_run += 1
                self._silence_run = 0
            else:
                self._silence_run += 1
            if self._ms(len(self._buffer)) >= self.max_segment_ms:
                return self._finish(forced=self._forced)
            if not self._forced and self._ms(self._silence_run) >= self.end_silence_ms:
                return self._finish(forced=False)
            return None

        self._pre_roll.append(frame)
        while self._ms(len(self._pre_roll)) > self.pre_roll_ms:
            self._pre_roll.popleft()
        if speech:
            self._speech_run += 1
            if self._speech_run >= self.start_frames:
                self._active = True
                self._buffer = list(self._pre_roll)
                self._silence_run = 0
                return SegmentEvent(SegmentKind.START)
        else:
            self._speech_run = 0
        return None

    def _finish(self, *, forced: bool) -> SegmentEvent:
        frames = self._buffer
        # Drop the trailing silence beyond one frame of hangover for a tighter utterance.
        trailing = min(self._silence_run, len(frames))
        keep = max(len(frames) - trailing + 1, 0) if not forced else len(frames)
        audio = (
            np.concatenate(frames[:keep]).astype(np.float32, copy=False)
            if keep > 0
            else np.zeros(0, dtype=np.float32)
        )
        duration_ms = int(round(audio.size * 1000 / self.sample_rate))
        self._active = False
        self._forced = False
        self._buffer = []
        self._speech_run = 0
        self._silence_run = 0
        self._pre_roll.clear()
        if duration_ms < self.min_segment_ms:
            return SegmentEvent(SegmentKind.ABORT, None, duration_ms, forced=forced)
        return SegmentEvent(SegmentKind.END, audio, duration_ms, forced=forced)
