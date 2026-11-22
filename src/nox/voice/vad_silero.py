"""Neural voice activity detection: Silero VAD over ONNX Runtime, with an energy fallback.

Why this exists: the energy detector in `nox.voice.vad` calls any loud sound "speech" - a game, a
television, music, a keyboard - and every such sound then becomes a segment that is queued for
transcription. Silero VAD (MIT, a ~1.2 MB network, about a tenth of a millisecond per 32 ms block
on one CPU thread) says whether a block of audio *sounds like speech*, which is the question the
capture loop actually has.

The model file is not downloaded and not trusted blindly. It is taken from, in this order, the
configured path, `<models_dir>/silero`, and the copy that ships inside the `faster-whisper`
package (the `voice` extra always has it), and its SHA-256 must be one of `KNOWN_MODEL_SHA256`
before ONNX Runtime ever parses it. A file that is missing, unknown, of an unexpected shape or
that fails to run is not an error for the user: `build_vad` hands back the energy detector and the
health line says why (`ResilientVad`).

How a frame becomes a decision:

- The network wants blocks of exactly 512 samples (32 ms at 16 kHz) with the 64 samples before
  them as context, and carries a recurrent state from block to block. Capture frames are 30 ms,
  so samples are collected and a block is evaluated whenever 512 are ready; a frame that completes
  no block repeats the previous probability.
- Hysteresis: speech starts at `threshold` and only ends below `threshold_off`, so a probability
  that hovers around a single threshold does not chop one sentence into pieces.
- Latency budget: every block is timed. A detector that needs more than `budget_ms` for
  `slow_blocks_to_degrade` blocks in a row (a starved CPU) is replaced by the energy detector
  rather than letting the capture loop fall behind real time.
- While there is no speech at all for `reset_after_silence_s`, the recurrent state is cleared, so
  a detector that runs for days starts every utterance from the same state.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from nox.core.events import HealthStatus
from nox.voice._logging import get_logger
from nox.voice.vad import EnergyVad, Vad

log = get_logger(__name__)

SAMPLE_RATE = 16000
#: The network's block: 32 ms at 16 kHz. Other lengths are not accepted by the model.
BLOCK_SAMPLES = 512
#: Samples of the previous block that are prepended to the next one.
CONTEXT_SAMPLES = 64
STATE_SHAPE = (1, 1, 128)
MODEL_FILE = "silero_vad_v6.onnx"
#: Models that may be loaded. The file is executed by ONNX Runtime, so an unknown hash is refused.
KNOWN_MODEL_SHA256: frozenset[str] = frozenset(
    {"4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"}
)
_EXPECTED_INPUTS = frozenset({"input", "h", "c"})


class SileroModelError(RuntimeError):
    """The neural detector cannot be used; the message says why, in words a log line can carry."""


class Session(Protocol):
    """The part of an `onnxruntime.InferenceSession` the detector uses (a fake in tests)."""

    def run(self, output_names: Any, input_feed: Mapping[str, np.ndarray]) -> Sequence[Any]: ...


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundled_model_path() -> Path | None:
    """The copy that ships inside `faster-whisper`, if that package is installed."""
    try:
        import faster_whisper
    except ImportError:
        return None
    path = Path(faster_whisper.__file__).resolve().parent / "assets" / MODEL_FILE
    return path if path.is_file() else None


def find_model(configured: str = "", models_dir: Path | None = None) -> Path:
    """The first existing model file; raises `SileroModelError` naming what was looked at."""
    candidates: list[Path] = []
    if configured:
        explicit = Path(configured)
        if not explicit.is_absolute() and models_dir is not None:
            explicit = models_dir / configured
        candidates.append(explicit)
    elif models_dir is not None:
        candidates.append(models_dir / MODEL_FILE)
    if not configured:
        bundled = bundled_model_path()
        if bundled is not None:
            candidates.append(bundled)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if configured:
        raise SileroModelError("the configured model file does not exist")
    raise SileroModelError(
        f"no {MODEL_FILE} found (neither under the models folder nor in faster-whisper)"
    )


def verify_model(path: Path, *, known: frozenset[str] = KNOWN_MODEL_SHA256) -> None:
    digest = sha256_of(path)
    if digest not in known:
        raise SileroModelError(f"the model file is not a known Silero VAD build (sha256 {digest})")


def open_session(path: Path) -> Session:
    """One ONNX Runtime session on one CPU thread, after the file's hash has been checked."""
    verify_model(path)
    try:
        import onnxruntime
    except ImportError as exc:
        raise SileroModelError("onnxruntime is not installed (voice extra)") from exc
    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    try:
        session = onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=options
        )
    except Exception as exc:  # noqa: BLE001 - ONNX Runtime raises its own error types
        raise SileroModelError(f"the model cannot be loaded: {type(exc).__name__}: {exc}") from exc
    names = {item.name for item in session.get_inputs()}
    if names != _EXPECTED_INPUTS:
        raise SileroModelError(f"unexpected model inputs {sorted(names)}")
    return session  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class SileroParams:
    #: Speech starts at this probability ...
    threshold: float = 0.5
    #: ... and stays on until the probability falls below this one.
    threshold_off: float = 0.35
    #: Time allowed for one block. Silero needs about 0.1 ms; this is the guard rail.
    budget_ms: float = 40.0
    slow_blocks_to_degrade: int = 25
    reset_after_silence_s: float = 10.0

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold_off <= self.threshold < 1.0:
            raise ValueError("need 0 < threshold_off <= threshold < 1")


class SileroVad:
    """Per-frame speech decision from the Silero network, with hysteresis.

    Frames may have any length: samples are collected into 512-sample blocks. The decision for a
    frame is made from the loudest-sounding-like-speech block completed during it (the previous
    probability if none completed).
    """

    id = "silero"

    def __init__(
        self,
        session: Session,
        params: SileroParams | None = None,
        *,
        clock: Callable[[], float] = time.perf_counter,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        if sample_rate != SAMPLE_RATE:
            raise ValueError("Silero VAD runs at 16 kHz")
        self._session = session
        self.params = params or SileroParams()
        self._clock = clock
        self._speech = False
        #: Probability of the latest evaluated block (0 before the first one).
        self.probability = 0.0
        #: Consecutive blocks over the time budget; `ResilientVad` reads this.
        self.slow_run = 0
        self.max_block_ms = 0.0
        self.blocks = 0
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._speech = False
        self.probability = 0.0
        self._quiet_samples = 0

    @property
    def over_budget(self) -> bool:
        """True once the budget was missed `slow_blocks_to_degrade` blocks in a row."""
        return self.slow_run >= self.params.slow_blocks_to_degrade

    def is_speech(self, frame: np.ndarray) -> bool:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        data = np.concatenate((self._pending, samples)) if self._pending.size else samples
        whole = data.size // BLOCK_SAMPLES
        best: float | None = None
        for index in range(whole):
            block = data[index * BLOCK_SAMPLES : (index + 1) * BLOCK_SAMPLES]
            probability = self._evaluate(block)
            best = probability if best is None else max(best, probability)
        self._pending = data[whole * BLOCK_SAMPLES :]
        if best is None:
            best = self.probability
        else:
            self.probability = best
        if best >= self.params.threshold:
            self._speech = True
        elif best < self.params.threshold_off:
            self._speech = False
        self._note_silence(samples.size, speech=self._speech)
        return self._speech

    def _evaluate(self, block: np.ndarray) -> float:
        row = np.concatenate((self._context, block)).reshape(1, -1).astype(np.float32, copy=False)
        started = self._clock()
        outputs = self._session.run(None, {"input": row, "h": self._h, "c": self._c})
        elapsed_ms = (self._clock() - started) * 1000.0
        probabilities, self._h, self._c = outputs[0], outputs[1], outputs[2]
        self._context = block[-CONTEXT_SAMPLES:].copy()
        self.blocks += 1
        self.max_block_ms = max(self.max_block_ms, elapsed_ms)
        self.slow_run = self.slow_run + 1 if elapsed_ms > self.params.budget_ms else 0
        return float(np.asarray(probabilities).reshape(-1)[-1])

    def _note_silence(self, samples: int, *, speech: bool) -> None:
        """Clear the recurrent state after a long stretch without any speech."""
        if speech:
            self._quiet_samples = 0
            return
        self._quiet_samples += samples
        if self._quiet_samples >= self.params.reset_after_silence_s * SAMPLE_RATE:
            self._h = np.zeros(STATE_SHAPE, dtype=np.float32)
            self._c = np.zeros(STATE_SHAPE, dtype=np.float32)
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
            self._quiet_samples = 0


class ResilientVad:
    """The neural detector with the energy detector behind it.

    Starts on `primary`; the first exception it raises, or a run of over-budget blocks, switches to
    `fallback` for good (the reason is logged once and shown by `health`). `primary=None` means the
    neural detector could not be built at all and `reason` says why.
    """

    def __init__(
        self,
        primary: SileroVad | None,
        fallback: Vad,
        *,
        reason: str = "",
        chosen: bool = False,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        #: True when the energy detector is what the user asked for, not a failure to report.
        self._chosen = chosen
        self._reason = "" if primary is not None else (reason or "no neural detector")

    @property
    def engine(self) -> str:
        return "silero" if self._primary is not None else "energy"

    @property
    def degraded_reason(self) -> str:
        return self._reason

    def health(self) -> tuple[HealthStatus, str]:
        if self._primary is not None:
            return HealthStatus.AVAILABLE, "speech detection: Silero VAD"
        if self._chosen:
            return HealthStatus.AVAILABLE, "speech detection: energy (chosen in the settings)"
        return HealthStatus.LIMITED, f"speech detection: energy fallback ({self._reason})"

    def reset(self) -> None:
        if self._primary is not None:
            self._primary.reset()
        self._fallback.reset()

    def is_speech(self, frame: np.ndarray) -> bool:
        primary = self._primary
        if primary is None:
            return self._fallback.is_speech(frame)
        try:
            decision = primary.is_speech(frame)
        except Exception as exc:  # noqa: BLE001 - any failure of the model hands over to energy
            self._degrade(f"{type(exc).__name__}: {exc}")
            return self._fallback.is_speech(frame)
        if primary.over_budget:
            self._degrade(
                f"{primary.slow_run} blocks in a row over {primary.params.budget_ms:g} ms"
            )
            return self._fallback.is_speech(frame)
        return decision

    def _degrade(self, reason: str) -> None:
        self._primary = None
        self._reason = reason
        self._fallback.reset()
        log.warning("voice.vad_degraded", using="energy", reason=reason)


def build_vad(
    *,
    engine: str = "silero",
    configured_model: str = "",
    models_dir: Path | None = None,
    params: SileroParams | None = None,
    fallback: Vad | None = None,
    open_model: Callable[[Path], Session] | None = None,
) -> ResilientVad:
    """The speech detector for the capture loop; never raises.

    `engine="energy"` is the deliberate choice of the simple detector; `silero` falls back to it,
    with the reason, when the model or the runtime is unusable.
    """
    energy = fallback if fallback is not None else EnergyVad()
    if engine == "energy":
        return ResilientVad(None, energy, reason="chosen in the settings", chosen=True)
    try:
        path = find_model(configured_model, models_dir)
        session = (open_model or open_session)(path)
        primary = SileroVad(session, params)
    except SileroModelError as exc:
        log.warning("voice.vad_unavailable", using="energy", reason=str(exc))
        return ResilientVad(None, energy, reason=str(exc))
    log.info("voice.vad_ready", engine="silero", model=path.name)
    return ResilientVad(primary, energy)
