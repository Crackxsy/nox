"""Acoustic wake-word gate in front of Whisper.

Continuous listening used to hand every VAD segment to Whisper `small` on CPU. With a game or a
video running, that queue grew to 20-40 s and Whisper happily "recognised" Dutch or English in
background noise. The gate puts a tiny always-on detector (openWakeWord, ONNX, a few hundred kB)
between the segmenter and the STT engine: a segment only reaches Whisper when

  (a) push-to-talk is held, or
  (b) the detector fired within the last `wake_window_s` seconds, or
  (c) a conversation window is still open (Nox was addressed a moment ago), or
  (d) the kill-phrase watchdog wants to look at a short segment.

Everything else is dropped before any transcription happens - which is a privacy property as much
as a CPU one: audio the user did not direct at Nox is never transcribed at all.

Honest limitation: openWakeWord ships pre-trained models for "hey jarvis", "alexa", "hey mycroft"
and friends, and there is no model for "Nox". Training one needs the `openwakeword[full]` toolchain
(torch/tensorflow) and is out of scope here, so the acoustic path is only used when the deployment
supplies its own model file under `<data_dir>/models/openwakeword/`. Without one the gate falls
back to the previous behaviour - every segment is transcribed and `WakeWordMatcher` does the
matching on the text - and reports `limited` with that reason. The kill phrase is the same story:
it has no acoustic model, so the watchdog in (d) still needs Whisper for short segments. Those
transcripts are examined for the kill phrase and then discarded; they never become an event and
never reach the language model.

Honest limitation, second part: that watchdog has a length ceiling
(`WakeGateConfig.kill_watchdog_max_ms`, 2.5 s by default). A kill phrase spoken *inside* a longer
utterance, with no wake word and no open conversation window, is dropped with the rest of that
utterance and never transcribed. The ceiling is what keeps "everything is transcribed anyway" from
coming back in through the watchdog, and the kill phrase remains reachable at any length through
push-to-talk, through the wake word and inside an open conversation window. Raising the ceiling
widens both the CPU cost and the amount of undirected speech that gets transcribed, so it is a
deliberate setting rather than a default to grow.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.models import engine_models_dir

log = get_logger(__name__)

DETECTOR_RATE = 16000
#: openWakeWord's melspectrogram front end works on 80 ms blocks at 16 kHz.
DETECTOR_BLOCK = 1280
MODEL_SUFFIXES = (".onnx", ".tflite")


class WakeWordDetector(Protocol):
    """A cheap always-on detector. `push` gets 16 kHz mono float32 and returns the best score."""

    id: str

    @property
    def acoustic(self) -> bool:
        """True when this detector really listens; False for the text-matching fallback."""
        ...

    def health(self) -> tuple[HealthStatus, str]: ...
    def push(self, audio: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class TextFallbackDetector:
    """No acoustic detection: every segment is passed on and matched on its transcript."""

    id = "text"

    def __init__(self, reason: str = "no openWakeWord model configured") -> None:
        self.reason = reason

    @property
    def acoustic(self) -> bool:
        return False

    def health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.LIMITED, (
            f"wake word matched on Whisper transcripts ({self.reason}); "
            "every segment is transcribed"
        )

    def push(self, audio: np.ndarray) -> float:
        return 0.0

    def reset(self) -> None:
        return None


def openwakeword_models(models_dir: Path, configured: str = "") -> list[Path]:
    """Model files for the acoustic detector: an explicit one, or every model in `models_dir`."""
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = models_dir / configured
        return [candidate] if candidate.is_file() else []
    if not models_dir.is_dir():
        return []
    return sorted(p for p in models_dir.iterdir() if p.suffix.lower() in MODEL_SUFFIXES)


class OpenWakeWordDetector:
    """openWakeWord (Apache-2.0) over ONNX Runtime, fed in 80 ms blocks."""

    id = "openwakeword"

    def __init__(self, model_paths: list[Path]) -> None:
        self.model_paths = model_paths
        self._model: Any = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._error = ""
        self.last_scores: dict[str, float] = {}

    @property
    def acoustic(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Build the openWakeWord model; raises so the caller can fall back to text matching."""
        from openwakeword.model import Model

        self._model = Model(
            wakeword_models=[str(p) for p in self.model_paths],
            inference_framework="onnx",
        )

    def health(self) -> tuple[HealthStatus, str]:
        if self._model is None:
            return HealthStatus.UNAVAILABLE, self._error or "not loaded"
        names = ", ".join(p.stem for p in self.model_paths)
        return HealthStatus.AVAILABLE, f"openWakeWord models: {names}"

    def push(self, audio: np.ndarray) -> float:
        model = self._model
        if model is None:
            return 0.0
        self._buffer = np.concatenate((self._buffer, audio.astype(np.float32, copy=False)))
        best = 0.0
        while self._buffer.size >= DETECTOR_BLOCK:
            block = self._buffer[:DETECTOR_BLOCK]
            self._buffer = self._buffer[DETECTOR_BLOCK:]
            pcm = (block * 32767.0).clip(-32768, 32767).astype(np.int16)
            try:
                scores = model.predict(pcm)
            except Exception as exc:  # noqa: BLE001 - a detector hiccup must not stop capture
                log.warning("voice.wake_detector_failed", error=f"{type(exc).__name__}: {exc}")
                return best
            self.last_scores = {str(k): float(v) for k, v in dict(scores).items()}
            best = max(best, max(self.last_scores.values(), default=0.0))
        return best

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self.last_scores = {}
        model = self._model
        if model is not None:
            reset = getattr(model, "reset", None)
            if reset is not None:
                reset()


def build_detector(
    *, engine: str, models_dir: Path | None = None, model: str = ""
) -> WakeWordDetector:
    """`openwakeword` when the package and a model are both usable, otherwise the text fallback."""
    if engine != "openwakeword":
        return TextFallbackDetector(reason="voice.stt.wake_word_engine is 'text'")
    directory = models_dir if models_dir is not None else engine_models_dir("openwakeword")
    paths = openwakeword_models(directory, model)
    if not paths:
        return TextFallbackDetector(
            reason=f"no wake-word model in {directory} (openWakeWord has no pre-trained 'Nox')"
        )
    detector = OpenWakeWordDetector(paths)
    try:
        detector.load()
    except Exception as exc:  # noqa: BLE001 - any failure degrades to text matching, never crashes
        reason = f"openWakeWord unavailable: {type(exc).__name__}: {exc}"
        log.warning("voice.wake_detector_unavailable", error=reason)
        return TextFallbackDetector(reason=reason)
    log.info("voice.wake_detector_ready", models=[p.name for p in paths])
    return detector


class GateDecision(StrEnum):
    PTT = "ptt"  # push-to-talk held: always transcribed
    WAKE = "wake_word"  # the acoustic detector fired recently
    CONVERSATION = "conversation"  # Nox was addressed a moment ago, the window is still open
    KILL_WATCHDOG = "kill_watchdog"  # short segment, checked for the kill phrase and then dropped
    TEXT_FALLBACK = "text_fallback"  # no acoustic detector: previous behaviour, everything passes
    DROP = "drop"

    @property
    def passes(self) -> bool:
        return self is not GateDecision.DROP

    @property
    def watchdog_only(self) -> bool:
        """True when the transcript may only be checked for the kill phrase, never reported."""
        return self is GateDecision.KILL_WATCHDOG


@dataclass
class WakeGateConfig:
    threshold: float = 0.5
    wake_window_s: float = 8.0
    conversation_window_s: float = 20.0
    kill_watchdog: bool = True
    #: Longest segment the kill-phrase watchdog will still hand to Whisper. Longer undirected
    #: speech is dropped untranscribed, so a kill phrase buried in a long sentence is not heard;
    #: see the module docstring for why this ceiling exists and what it costs.
    kill_watchdog_max_ms: int = 2500


@dataclass
class WakeGate:
    """Decides, per utterance, whether Whisper is allowed to see it."""

    detector: WakeWordDetector = field(default_factory=TextFallbackDetector)
    config: WakeGateConfig = field(default_factory=WakeGateConfig)
    clock: Callable[[], float] = time.monotonic

    _last_fire: float = field(default=-1e9, init=False)
    _conversation_until: float = field(default=-1e9, init=False)
    fires: int = field(default=0, init=False)
    #: Why segments never became a transcript event, by reason. One counter, so a health or stats
    #: readout cannot mix "dropped before Whisper" with "transcribed only for the watchdog".
    drops: dict[str, int] = field(default_factory=dict, init=False)

    @property
    def dropped(self) -> int:
        """Segments dropped before Whisper saw them."""
        return self.drops.get("gated_out", 0)

    @property
    def not_reported(self) -> int:
        """Every segment that did not become a transcript event, for any gate reason."""
        return sum(self.drops.values())

    def note_not_reported(self, reason: str) -> None:
        """Record why a segment did not become a transcript event (`gated_out`,
        `watchdog_discarded`)."""
        self.drops[reason] = self.drops.get(reason, 0) + 1

    @property
    def acoustic(self) -> bool:
        return self.detector.acoustic

    def health(self) -> tuple[HealthStatus, str]:
        return self.detector.health()

    def feed(self, frame: np.ndarray) -> bool:
        """Push one 16 kHz mono frame through the detector. True when the wake word just fired."""
        return self.feed_many([frame])

    def feed_many(self, frames: list[np.ndarray]) -> bool:
        """Push several consecutive frames at once. True when the wake word just fired.

        The pipeline batches whatever frames queued up while the previous inference ran, so the
        detector is invoked once per batch in a worker thread instead of once per 30 ms frame on
        the event loop. The detector keeps its own 80 ms block buffer, so batching does not change
        what it sees - only how often it is called.
        """
        if not self.detector.acoustic or not frames:
            return False
        block = frames[0] if len(frames) == 1 else np.concatenate(frames)
        score = self.detector.push(block)
        if score < self.config.threshold:
            return False
        # A detection usually spans several 80 ms blocks; count and log the start of it, not every
        # block above the threshold.
        now = self.clock()
        renewed = now - self._last_fire <= self.config.wake_window_s
        self._last_fire = now
        if renewed:
            return True
        self.fires += 1
        log.info("voice.wake_word_detected", score=round(score, 3))
        return True

    def note_addressed(self) -> None:
        """Nox was addressed: keep the gate open for a follow-up question without a wake word."""
        self._conversation_until = self.clock() + self.config.conversation_window_s

    def close_conversation(self) -> None:
        self._conversation_until = -1e9

    @property
    def conversation_open(self) -> bool:
        return self.clock() < self._conversation_until

    def decide(self, *, duration_ms: int, ptt: bool) -> GateDecision:
        if ptt:
            return GateDecision.PTT
        if not self.detector.acoustic:
            return GateDecision.TEXT_FALLBACK
        if self.clock() - self._last_fire <= self.config.wake_window_s:
            return GateDecision.WAKE
        if self.conversation_open:
            return GateDecision.CONVERSATION
        if self.config.kill_watchdog and duration_ms <= self.config.kill_watchdog_max_ms:
            return GateDecision.KILL_WATCHDOG
        self.note_not_reported("gated_out")
        return GateDecision.DROP

    def reset(self) -> None:
        self._last_fire = -1e9
        self._conversation_until = -1e9
        self.detector.reset()
