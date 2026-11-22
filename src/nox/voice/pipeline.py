"""DefaultVoicePipeline: mic -> VAD -> wake word / PTT -> STT -> events; say -> TTS -> output.

Runs inside the voice worker; results leave through the injected `emit(name, payload)` callback,
which the worker maps to IPC events. Security gate: no frame is processed unless `capture_allowed`
is true, the pipeline is not muted and no kill phrase has been latched. The kill phrase is reported
as `voice.kill_phrase` and never reaches the language model - it produces no transcript event. Raw
audio only ever lives in memory.

Second gate: between the segmenter and the STT engine sits the wake-word gate
(`nox.voice.stt.wake_gate`). While no push-to-talk is held and no conversation window is open, a
segment only reaches Whisper when the cheap acoustic detector fired recently; otherwise it is
dropped without being transcribed at all. `listening_mode: ptt_only` goes one step further and
keeps the capture device closed until push-to-talk is actually held.

Two loops run beside the capture loop so the event loop is never the bottleneck: the wake-word
detector's ONNX inference runs in a worker thread over batches of queued frames, and transcription
runs one utterance at a time through a single-consumer queue, which also keeps transcript events in
utterance order.

The capture loop runs under a supervisor (`nox.voice.capture_health`): a device that disappears is
reopened with a growing delay, and the user is told once when Nox cannot hear and once when it can
again. Speech is organised in generations: an interruption ends the current generation, so every
sentence that was already accepted for it - playing or waiting its turn - is dropped, while a
sentence requested afterwards belongs to the next generation and plays normally. Each voice turn is
timed from the end of the utterance to the first audio chunk (`nox.voice.latency`).

Speech is one stream, not a series of separate plays: while a sentence is heard, the next ones that
are already waiting are synthesised ahead (`nox.voice.prefetch`: a bounded look-ahead that only
runs while the machine has room), their edges are faded through silence (`nox.voice.edges`), and the
silence between the end of one sentence and the start of the next is measured as the sentence gap.
An interruption cancels that look-ahead on the spot. Talking over Nox needs more than a detector
trigger (`nox.voice.barge_in`): enough loud speech, so noise and echo do not cut a turn short.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from nox.core.events import E, HealthStatus, TranscriptReady, TtsStarted, VoiceKillPhrase
from nox.util.aio import aclose
from nox.voice._logging import get_logger
from nox.voice.audio import AudioUnavailableError
from nox.voice.barge_in import DEFAULT_MIN_DB, DEFAULT_MIN_MS, BargeInGate
from nox.voice.base import AudioInput, AudioOutput, SttEngine, TtsEngine, TtsRequest
from nox.voice.capture_health import (
    CaptureMonitor,
    CaptureNotice,
    CaptureSupervisor,
)
from nox.voice.edges import MAX_FADE_MS, SILENCE_THRESHOLD_DB, fade_edges, trim_silence
from nox.voice.latency import LatencySummary, LatencyTracker, TurnTicket, noting_first_chunk
from nox.voice.levels import pace_levels
from nox.voice.prefetch import BYTES_PER_SAMPLE, MAX_LOOKAHEAD, PrefetchedSpeech
from nox.voice.stt.wake_gate import GateDecision, WakeGate
from nox.voice.stt.wake_word import WakeWordMatcher
from nox.voice.tts.clips import ClipNotFoundError
from nox.voice.tts.normalize import normalize_for_speech
from nox.voice.vad import EnergyVad, Segmenter, SegmentEvent, SegmentKind

log = get_logger(__name__)

#: How long `stop` waits for a reopen that holds the device lock before it closes the input anyway.
DEVICE_LOCK_TIMEOUT_S = 2.0

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
CaptureAllowed = Callable[[], bool]


class PipelineConfig(BaseModel):
    wake_word: str = "Nox"
    language: str = "auto"
    barge_in: bool = True
    end_silence_ms: int = Field(default=600, ge=100)
    max_utterance_ms: int = Field(default=15000, ge=1000)
    min_utterance_ms: int = Field(default=250, ge=0)
    require_wake_word: bool = False  # True: speech without wake word / PTT is not reported at all
    #: `continuous` keeps the capture device open and relies on the wake-word gate; `ptt_only`
    #: keeps it closed - and the operating system's microphone indicator off - until
    #: push-to-talk is held.
    listening_mode: Literal["continuous", "ptt_only"] = "continuous"
    #: How many waiting sentences are synthesised while the one before them is heard. 0 switches
    #: the look-ahead off: every sentence is synthesised when its turn comes.
    prefetch_sentences: int = Field(default=1, ge=0, le=MAX_LOOKAHEAD)
    #: The most audio one look-ahead sentence may buffer, in seconds: the synthesis waits when
    #: it is that far ahead of the device, so memory stays small however fast the engine is.
    prefetch_buffer_s: float = Field(default=20.0, gt=0.0, le=120.0)
    #: Fade of the first and last milliseconds of every sentence, so a stream that stays open
    #: between sentences has no click at the seam. 0 switches it off.
    edge_fade_ms: float = Field(default=4.0, ge=0.0, le=MAX_FADE_MS)
    #: Cut the silence an engine leaves in front of and behind a sentence (`trim_silence`), so
    #: the first sound is heard as early as it exists and sentences follow each other closely.
    trim_silence: bool = True
    silence_threshold_db: float = Field(default=SILENCE_THRESHOLD_DB, ge=-90.0, le=-20.0)
    #: Write numbers, dates, units and abbreviations out in words before the engine reads them.
    normalize_text: bool = True
    #: Talking over Nox: how much loud speech cuts the answer off, and how loud is loud
    #: (`nox.voice.barge_in` explains the defaults). 0 ms means any detected speech does.
    barge_in_min_ms: int = Field(default=DEFAULT_MIN_MS, ge=0, le=2000)
    barge_in_min_db: float = Field(default=DEFAULT_MIN_DB, ge=-90.0, le=0.0)


@dataclass(slots=True)
class _Speech:
    """One sentence on its way to the speakers: asked for, maybe synthesised ahead, then played."""

    request: TtsRequest
    #: The generation it was requested in; an interruption makes it stale.
    generation: int
    #: Set for the first sentence of a timed voice turn.
    ticket: TurnTicket | None
    #: Another sentence was playing or waiting when this one was asked for: the silence before it
    #: is a sentence gap, which is measured. A sentence that opens an answer has no gap.
    queued_behind: bool
    audio: PrefetchedSpeech = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Utterance:
    """One finished segment waiting for its turn on the single transcription slot."""

    audio: np.ndarray
    forced: bool
    duration_ms: int
    #: When the end-of-speech decision was made, on the pipeline's latency clock.
    ended_at: float


class DefaultVoicePipeline:
    def __init__(
        self,
        *,
        audio_in: AudioInput,
        audio_out: AudioOutput,
        stt: SttEngine,
        tts: TtsEngine,
        emit: Emit,
        capture_allowed: CaptureAllowed,
        config: PipelineConfig | None = None,
        segmenter: Segmenter | None = None,
        wake_gate: WakeGate | None = None,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        input_devices: Callable[[], Sequence[str]] | None = None,
        device_lock_timeout_s: float = DEVICE_LOCK_TIMEOUT_S,
        device_lock_wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prefetch_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        """`clock` and `sleep` are injected for tests. `input_devices` lists the input devices'
        names: a change while the microphone is down makes the supervisor try again at once.
        `device_lock_wait` is how `stop` waits out `device_lock_timeout_s` for the device lock.
        `prefetch_allowed` says whether the machine has room to synthesise a sentence before its
        turn (`nox.voice.prefetch.ResourceGate`); when it says no, the sentence waits."""
        self._audio_in = audio_in
        self._audio_out = audio_out
        self._stt = stt
        self._tts = tts
        self._emit = emit
        self._capture_allowed = capture_allowed
        self.config = config or PipelineConfig()
        self._segmenter = segmenter or Segmenter(
            vad=EnergyVad(),
            sample_rate=audio_in.sample_rate,
            end_silence_ms=self.config.end_silence_ms,
            max_segment_ms=self.config.max_utterance_ms,
            min_segment_ms=self.config.min_utterance_ms,
        )
        self._wake = WakeWordMatcher(self.config.wake_word)
        # No gate passed in = the text fallback: every segment is transcribed and the wake word is
        # matched on the transcript instead.
        self._gate = wake_gate or WakeGate()
        self._muted = False
        self._kill_latched = False
        self._stopped = False
        self._started = False
        self._speaking = False
        self._ptt_active = False
        self._clock = clock
        self._device_lock_timeout_s = device_lock_timeout_s
        self._device_lock_wait = device_lock_wait
        #: Raised by every interruption. A sentence belongs to the generation it was requested in
        #: and is dropped, not played, once the pipeline has moved on to a newer one.
        self._generation = 0
        #: Why the latest interruption happened. Reported on the `tts.interrupted` of whatever it
        #: cut off; deliberately not cleared by later sentences.
        self._interrupt_reason = ""
        #: Sentences requested and not yet finished: playing, or waiting for the speech lock.
        self._inflight = 0
        #: Sentences asked for and not yet playing, in the order they will be heard.
        self._waiting: list[_Speech] = []
        #: The sentence that is being played now.
        self._playing: _Speech | None = None
        self._prefetch_allowed = prefetch_allowed
        #: When the last sentence of the current answer finished playing; the next sentence's
        #: first chunk measures the sentence gap against it. None whenever there is no gap to
        #: measure (nothing played, or the answer was interrupted).
        self._last_audio_end: float | None = None
        self._say_lock = asyncio.Lock()
        self._notice_tasks: set[asyncio.Task[None]] = set()
        self._latency = LatencyTracker(clock=clock)
        self._device_lock = asyncio.Lock()
        self._capture = CaptureMonitor(clock=clock)
        self._supervisor = CaptureSupervisor(
            monitor=self._capture,
            listen=self._listen,
            is_active=self.capture_active,
            reopen=self._reopen_capture,
            announce=self._announce_capture,
            stopped=lambda: self._stopped,
            clock=clock,
            sleep=sleep,
            input_devices=input_devices,
        )
        self._detect_task: asyncio.Task[None] | None = None
        self._transcribe_task: asyncio.Task[None] | None = None
        self._detect_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._pending: asyncio.Queue[_Utterance] = asyncio.Queue()
        self._barge = BargeInGate(
            min_ms=self.config.barge_in_min_ms,
            min_db=self.config.barge_in_min_db,
            frame_ms=self._segmenter.frame_ms,
        )
        self.frames_seen = 0
        self.utterances = 0

    # ---- state -----------------------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def _busy_speaking(self) -> bool:
        """Something is playing, or sentences are waiting for their turn to play."""
        return bool(self._speaking or self._inflight)

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def kill_latched(self) -> bool:
        return self._kill_latched

    @property
    def wake_gate(self) -> WakeGate:
        return self._gate

    @property
    def gated_out(self) -> int:
        """Segments that never became a transcript event; the gate counts them by reason."""
        return self._gate.not_reported

    def wake_gate_health(self) -> tuple[HealthStatus, str]:
        return self._gate.health()

    def capture_health(self) -> tuple[HealthStatus, str]:
        """Whether the capture path works: `unavailable`, with the reason, from the moment the
        microphone fails until audio flows again."""
        if self._capture.failing:
            return self._capture.health()
        if not self._started:
            return HealthStatus.UNAVAILABLE, "capture not started"
        return HealthStatus.AVAILABLE, "capturing"

    def latency_summary(self) -> LatencySummary:
        """P50/P95 of the last turns, from the end of the utterance to the first audio chunk."""
        return self._latency.summary()

    def gate_open(self) -> bool:
        """True when capture is permitted right now (privacy state, mute, kill latch, running)."""
        if self._stopped or self._muted or self._kill_latched:
            return False
        return bool(self._capture_allowed())

    def capture_active(self) -> bool:
        """`gate_open` plus the listening mode: `ptt_only` only captures while PTT is held."""
        if not self.gate_open():
            return False
        if self.config.listening_mode == "ptt_only":
            return self._ptt_active
        return True

    async def refresh_gate(self) -> None:
        """Re-evaluate the capture gate (call after a privacy, mute or kill-state change).

        This is what opens and closes the capture device, which is why it is async: in `ptt_only`
        the device is not merely ignored while PTT is up, it is not open at all.
        """
        active = self.capture_active()
        async with self._device_lock:
            await self._set_device_enabled(active)
        if not active and self._segmenter.active:
            self._segmenter.reset()
        if not active:
            self._gate.reset()

    async def _set_device_enabled(self, active: bool) -> None:
        """Open or close the device. Opening can fail (unplugged, in use): that becomes a health
        state and a retry by the supervisor, not an error for whoever changed the gate."""
        try:
            await self._audio_in.set_enabled(active)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported through health, retried by the supervisor
            if not active:
                raise
            self._supervisor.report_open_failure(
                f"cannot open the microphone: {type(exc).__name__}: {exc}"
            )

    async def _acquire_device(self) -> bool:
        """Take the device lock, waiting at most `device_lock_timeout_s`; False when it timed out.

        The timeout is its own injected wait (`device_lock_wait`), not the supervisor's `sleep`:
        a test that moves the supervisor's clock must not make this one expire on the way.
        """
        if not self._device_lock.locked():
            await self._device_lock.acquire()
            return True
        acquire = asyncio.ensure_future(self._device_lock.acquire())
        timeout = asyncio.ensure_future(self._device_lock_wait(self._device_lock_timeout_s))
        try:
            await asyncio.wait({acquire, timeout}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timeout.cancel()
        if acquire.done() and not acquire.cancelled():
            return True
        acquire.cancel()
        await asyncio.gather(acquire, return_exceptions=True)
        return not acquire.cancelled()  # it may have got the lock in the very moment of the cancel

    async def _with_device_lock(self, action: Callable[[], Awaitable[None]]) -> None:
        """Run `action` holding the device lock - or, when the holder does not let go in time,
        run it anyway: closing the microphone is the point of every caller, and it matters more
        than the exclusion the lock gives."""
        held = await self._acquire_device()
        if not held:
            log.warning("voice.device_lock_timeout", seconds=self._device_lock_timeout_s)
        try:
            await action()
        finally:
            if held:
                self._device_lock.release()

    async def _reopen_capture(self) -> None:
        """Supervisor restart: close the input completely, open it again, re-apply the gate.

        Raises when the device still cannot be opened; the supervisor counts that as a failure.
        """
        async with self._device_lock:
            try:
                await self._audio_in.set_enabled(False)
                await self._audio_in.stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - closing a device that is gone may well fail
                log.debug("voice.capture_close_failed", error=f"{type(exc).__name__}: {exc}")
            await self._audio_in.start()
            if self._segmenter.active:
                self._segmenter.reset()
            self._gate.reset()
            # A stop that did not wait for this reopen (see `stop`) must not be undone by it.
            await self._audio_in.set_enabled(self.capture_active() and not self._stopped)

    async def reset_kill(self) -> None:
        self._kill_latched = False
        await self.refresh_gate()

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._stopped = False
        self._started = True
        self._capture.reset()
        await self._audio_in.start()
        await self.refresh_gate()
        self._supervisor.start()
        self._detect_task = asyncio.create_task(self._detect_loop(), name="voice-wake-detect")
        self._transcribe_task = asyncio.create_task(
            self._transcribe_loop(), name="voice-transcribe"
        )
        status, reason = self._gate.health()
        log.info(
            "voice.pipeline_started",
            gate_open=self.gate_open(),
            listening_mode=self.config.listening_mode,
            wake_word_engine=self._gate.detector.id,
            wake_word_health=f"{status}: {reason}",
        )

    async def stop(self) -> None:
        self._stopped = True
        self._started = False
        # Under the device lock, so a reopen that is opening the device right now finishes first
        # and is closed here, instead of leaving a stream open behind a stopped pipeline. The wait
        # is bounded: stop is what the kill switch calls, and a device that hangs while it opens
        # must not hang the kill switch with it.
        await self._with_device_lock(lambda: self._audio_in.set_enabled(False))
        await self.interrupt(reason="stop")
        await self._with_device_lock(self._audio_in.stop)
        await self._supervisor.stop()
        tasks = [
            t
            for t in (self._detect_task, self._transcribe_task, *self._notice_tasks)
            if t is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._detect_task = self._transcribe_task = None
        self._segmenter.reset()
        self._latency.abandon()
        log.info("voice.pipeline_stopped")

    # ---- capture side ----------------------------------------------------------------------------

    async def _listen(self) -> None:
        """Pull frames from the capture device until it ends or fails.

        Runs under the capture supervisor, which restarts it: a loop that ended or raised is a
        failure it records and retries, not the end of listening.
        """
        async for frame in self._audio_in.frames():
            if self._stopped:
                return
            if not self.capture_active():
                # Defense in depth: even if the device still delivers frames, drop them here.
                await self._audio_in.set_enabled(False)
                if self._segmenter.active:
                    self._segmenter.reset()
                continue
            self._supervisor.note_frame()
            self.frames_seen += 1
            # The detector sees every frame, so a wake word spoken *before* the segment opened
            # still counts; the inference itself happens in `_detect_loop`, off this loop.
            self._detect_queue.put_nowait(frame)
            event = self._segmenter.push(frame)
            if event is not None:
                await self._on_segment(event)
            await self._watch_for_barge_in(frame)

    async def _watch_for_barge_in(self, frame: np.ndarray) -> None:
        """While a segment that opened over Nox's speech is open: is the user really talking?"""
        if not self._barge.armed:
            return
        if not (self._busy_speaking and self.config.barge_in and self._segmenter.active):
            self._barge.disarm()  # nothing left to talk over, or the segment is gone
            return
        if self._barge.observe(frame):
            await self.interrupt(reason="barge_in")

    def _announce_capture(self, notice: CaptureNotice) -> None:
        """Tell the core once that the microphone is gone, unstable or back (never per retry).

        The worker does not speak it: an unprompted sentence is the core's to allow (quiet hours,
        privacy mode and zones, game mode - `SpeechPolicy`), which this process does not know.
        """
        if self._stopped or self._kill_latched:
            return
        payload = {"notice": str(notice), "reason": self._capture.reason}
        task = asyncio.create_task(self._send_notice(payload), name="voice-capture-notice")
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _send_notice(self, payload: dict[str, Any]) -> None:
        try:
            await self._emit(E.VOICE_CAPTURE_NOTICE, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the health state still says it
            log.warning("voice.capture_notice_failed", error=f"{type(exc).__name__}: {exc}")

    async def _detect_loop(self) -> None:
        """Run the wake-word detector in a worker thread over whatever frames have queued up.

        openWakeWord is ONNX inference: small, but still CPU work that has no business running on
        the event loop 33 times a second. Batching whatever arrived since the last pass keeps the
        wake-word latency at one scheduling hop without ever blocking the loop.
        """
        while True:
            batch = [await self._detect_queue.get()]
            while not self._detect_queue.empty():
                batch.append(self._detect_queue.get_nowait())
            try:
                await asyncio.to_thread(self._gate.feed_many, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a detector hiccup must not stop capture
                log.warning("voice.wake_detect_failed", error=f"{type(exc).__name__}: {exc}")

    async def _on_segment(self, event: SegmentEvent) -> None:
        if event.kind == SegmentKind.START:
            await self._emit(E.VOICE_INPUT_STARTED, {"forced": event.forced})
            if self._busy_speaking and self.config.barge_in:
                if event.forced:
                    await self.interrupt(reason="barge_in")  # the key press is the user's word
                else:
                    self._barge.arm()  # a detector trigger alone is not enough: see `BargeInGate`
        elif event.kind == SegmentKind.END and event.audio is not None:
            self._barge.disarm()
            ended_at = self._clock()
            await self._emit(E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms})
            self._pending.put_nowait(
                _Utterance(
                    audio=event.audio,
                    forced=event.forced,
                    duration_ms=event.duration_ms,
                    ended_at=ended_at,
                )
            )
        elif event.kind == SegmentKind.ABORT:
            self._barge.disarm()
            await self._emit(
                E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms, "dropped": True}
            )

    async def _transcribe_loop(self) -> None:
        """One utterance at a time: bounds the CPU Whisper may use and keeps events in order."""
        while True:
            utterance = await self._pending.get()
            try:
                await self._process_utterance(
                    utterance.audio,
                    forced=utterance.forced,
                    duration_ms=utterance.duration_ms,
                    ended_at=utterance.ended_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad utterance must not end the loop
                log.error("voice.utterance_failed", error=f"{type(exc).__name__}: {exc}")

    async def _process_utterance(
        self,
        audio: np.ndarray,
        *,
        forced: bool,
        duration_ms: int,
        ended_at: float,
    ) -> None:
        if not self.gate_open() and not forced:
            return
        decision = self._gate.decide(duration_ms=duration_ms, ptt=forced or self._ptt_active)
        if decision is GateDecision.DROP:
            # Whisper never sees this audio: no transcript, no event, and no log line carrying any
            # content - only the fact that a segment was dropped (counted by the gate itself).
            log.debug("voice.gated_out", duration_ms=duration_ms)
            return
        self.utterances += 1
        try:
            transcript = await self._stt.transcribe(
                audio, self._audio_in.sample_rate, language=self.config.language
            )
        except Exception as exc:  # noqa: BLE001 - engine failure must not kill the listen loop
            log.error("voice.stt_failed", error=f"{type(exc).__name__}: {exc}")
            return
        transcribed_at = self._clock()
        text = transcript.text.strip()
        if not text:
            return
        match = self._wake.match(text)
        if decision.watchdog_only and not match.kill:
            # The segment only got through so the kill phrase stays reachable without an acoustic
            # model for it; anything else is discarded here and never becomes an event.
            self._gate.note_not_reported("watchdog_discarded")
            log.debug("voice.watchdog_discarded", duration_ms=transcript.duration_ms)
            return
        if match.kill:
            self._kill_latched = True
            await self.refresh_gate()
            await self.interrupt(reason="kill_phrase")
            payload = VoiceKillPhrase(by="voice", language=transcript.language).model_dump(
                mode="json"
            )
            await self._emit(E.VOICE_KILL_PHRASE, payload)
            log.warning("voice.kill_phrase")
            return
        addressed = forced or match.addressed or decision is GateDecision.WAKE
        if not addressed and self.config.require_wake_word:
            log.debug("voice.unaddressed_dropped", duration_ms=transcript.duration_ms)
            return
        if addressed:
            # Follow-up questions may skip the wake word while the conversation window is open.
            self._gate.note_addressed()
        ready = TranscriptReady(
            text=match.remainder if match.addressed else text,
            language=transcript.language,
            confidence=transcript.confidence,
            addressed_to_nox=addressed,
            duration_ms=transcript.duration_ms,
            latency_ms=transcript.latency_ms,
        )
        await self._emit(E.VOICE_TRANSCRIPT_READY, ready.model_dump(mode="json"))
        if addressed:
            self._latency.begin(
                ended_at=ended_at,
                transcribed_at=transcribed_at,
                published_at=self._clock(),
                endpoint_ms=0.0 if forced else float(self.config.end_silence_ms),
            )

    async def push_to_talk(self, pressed: bool) -> None:
        if pressed:
            if not self.gate_open():
                log.info("voice.ptt_refused", reason="capture not allowed")
                return
            if self._ptt_active:
                return
            self._ptt_active = True
            # In `ptt_only` this is where the capture device is opened, and nowhere earlier.
            await self.refresh_gate()
            await self._emit(E.VOICE_PTT_PRESSED, {})
            event = self._segmenter.force_start()
            if event is not None:
                await self._on_segment(event)
            elif self._busy_speaking and self.config.barge_in:
                await self.interrupt(reason="barge_in")
            return
        if not self._ptt_active:
            return
        self._ptt_active = False
        await self._emit(E.VOICE_PTT_RELEASED, {})
        event = self._segmenter.force_end()
        if event is not None:
            await self._on_segment(event)
        await self.refresh_gate()  # closes the capture device again in `ptt_only`

    async def set_muted(self, muted: bool) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        await self.refresh_gate()
        await self._emit(E.VOICE_MUTED, {"muted": muted})

    # ---- speaking side ---------------------------------------------------------------------------

    async def say(self, request: TtsRequest) -> None:
        """Speak one sentence, unless an interruption overtakes it before its turn comes.

        The generation is read on entry, before waiting for the speech lock: an interruption that
        happens while this sentence waits ends its generation and drops it, however many sentences
        are queued behind the one that was playing. While it waits, its audio may already be
        synthesised (`_plan_lookahead`). The first sentence after a voice turn's transcript is
        timed (`nox.voice.latency`).
        """
        generation = self._generation
        speech = _Speech(
            request=request,
            generation=generation,
            ticket=self._latency.say_requested(),
            queued_behind=self._busy_speaking,
            audio=PrefetchedSpeech(
                lambda: self._synthesize(request),
                max_bytes=max(
                    BYTES_PER_SAMPLE,
                    int(self.config.prefetch_buffer_s * self._tts.sample_rate * BYTES_PER_SAMPLE),
                ),
                name=request.utterance_id,
            ),
        )
        self._inflight += 1
        self._waiting.append(speech)
        self._plan_lookahead()
        try:
            async with self._say_lock:
                self._waiting.remove(speech)
                if self._stopped:
                    return
                if generation != self._generation:
                    await self._drop(request)
                    return
                self._plan_lookahead()  # this one starts to play: the one after it may be prepared
                await self._speak(speech)
        finally:
            if speech in self._waiting:
                self._waiting.remove(speech)
            await speech.audio.cancel()
            self._inflight -= 1

    def _plan_lookahead(self) -> None:
        """Start synthesising the sentences that will be heard next, as far as allowed.

        At most `prefetch_sentences` of the waiting ones, in the order they will play, and only
        while the machine has room. A sentence of an interrupted generation is never prepared. One
        that is not prepared here is synthesised when its turn comes - the look-ahead only ever
        removes waiting, it is not something a sentence depends on.
        """
        limit = self.config.prefetch_sentences
        for speech in self._waiting[:limit]:
            if speech.audio.started or speech.generation != self._generation:
                continue
            if not self._prefetch_allowed():
                return
            speech.audio.start()

    async def _drop(self, request: TtsRequest) -> None:
        """A sentence the user interrupted before it started: it ends like any interrupted one."""
        log.debug("voice.say_dropped", utterance_id=request.utterance_id)
        await self._emit(
            E.TTS_INTERRUPTED,
            {"utterance_id": request.utterance_id, "reason": self._interrupt_reason},
        )

    async def _speak(self, speech: _Speech) -> None:
        request, generation = speech.request, speech.generation
        started = TtsStarted(
            text=request.text,
            channel=str(request.channel),
            engine=self._tts.id,
            utterance_id=request.utterance_id,
        )
        # Set before the await: a barge-in landing while `tts.started` is still in flight must
        # interrupt this utterance, not be dropped as "not speaking yet".
        self._speaking = True
        self._playing = speech
        reported = False
        pacer: asyncio.Task[None] | None = None
        streams: list[AsyncIterator[bytes]] = []
        try:
            await self._emit(E.TTS_STARTED, started.model_dump(mode="json"))
            if generation != self._generation:
                return  # interrupted before the first sample; `finally` reports it
            heard = speech.audio.chunks()
            streams.append(heard)
            if self.config.trim_silence:
                heard = trim_silence(
                    heard,
                    self._tts.sample_rate,
                    threshold_db=self.config.silence_threshold_db,
                )
                streams.append(heard)
            if self.config.edge_fade_ms > 0:
                heard = fade_edges(heard, self._tts.sample_rate, self.config.edge_fade_ms)
                streams.append(heard)
            heard = noting_first_chunk(heard, lambda: self._note_first_audio(speech))
            streams.append(heard)
            pacer = (
                asyncio.create_task(pace_levels(self._audio_out, self._emit, request))
                if hasattr(self._audio_out, "level") and str(request.channel) != "mute"
                else None
            )
            await self._audio_out.play(
                heard, self._tts.sample_rate, request.channel, utterance_id=request.utterance_id
            )
            # The gap to the next sentence starts here, before any of the bookkeeping below.
            self._last_audio_end = self._clock() if generation == self._generation else None
        except (AudioUnavailableError, FileNotFoundError, ValueError) as exc:
            log.error("voice.tts_failed", utterance_id=request.utterance_id, error=str(exc))
            reported = True
            self._last_audio_end = None
            await self._finish(request, ok=False, reason=type(exc).__name__)
        except Exception as exc:
            # Anything else is still an utterance the user did not hear. Report it, then let
            # the caller see the original error.
            log.error(
                "voice.tts_failed",
                utterance_id=request.utterance_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            reported = True
            self._last_audio_end = None
            await self._finish(request, ok=False, reason=type(exc).__name__)
            raise
        finally:
            if pacer is not None:
                pacer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pacer
            self._speaking = False
            self._playing = None
            for stream in reversed(streams):
                await aclose(stream)
            await speech.audio.cancel()  # a synthesis still running for a cut-off sentence ends now
            if not reported:
                await self._finish(
                    request, ok=True, reason="", interrupted=generation != self._generation
                )

    def _note_first_audio(self, speech: _Speech) -> None:
        """The first chunk of `speech` reached the output: time the turn, and time the gap."""
        now = self._clock()
        if speech.ticket is not None:
            result = self._latency.first_audio(speech.ticket)
            if result is not None:
                log.info("voice.turn_latency", **asdict(result))
        ended = self._last_audio_end
        self._last_audio_end = None
        if speech.queued_behind and ended is not None:
            gap = self._latency.record_gap(ended, now)
            log.debug("voice.sentence_gap", gap_ms=gap, prefetched=speech.audio.was_ahead)

    async def _synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        """The engine's audio for `request`. A prepared clip that is not installed is spoken from
        the request's own text instead of failing: callouts name a clip and carry their words, and
        no clip ships with Nox, so the alternative to speaking the words is silence.

        The engine is given the words as they are said (numbers, dates and abbreviations written
        out); every other field of the request, the style included, goes through unchanged."""
        if self.config.normalize_text and request.normalize:
            words = normalize_for_speech(request.text, request.language)
            if words != request.text:
                request = request.model_copy(update={"text": words})
        first = self._tts.synthesize(request)
        started = False
        try:
            async for chunk in first:
                started = True
                yield chunk
            return
        except ClipNotFoundError:
            if started or not request.prepared_clip or not request.text.strip():
                raise
            log.info(
                "voice.clip_missing_spoken",
                utterance_id=request.utterance_id,
                clip_id=request.prepared_clip,
            )
        finally:
            await aclose(first)
        spoken = self._tts.synthesize(request.model_copy(update={"prepared_clip": None}))
        try:
            async for chunk in spoken:
                yield chunk
        finally:
            await aclose(spoken)

    async def _finish(
        self, request: TtsRequest, *, ok: bool, reason: str, interrupted: bool = False
    ) -> None:
        """Exactly one terminal event per utterance, so no `say` ever dangles."""
        if ok and interrupted:
            await self._emit(
                E.TTS_INTERRUPTED,
                {"utterance_id": request.utterance_id, "reason": self._interrupt_reason},
            )
            return
        payload: dict[str, Any] = {"utterance_id": request.utterance_id, "ok": ok}
        if not ok:
            payload["reason"] = reason
        await self._emit(E.TTS_FINISHED, payload)

    async def interrupt(self, *, reason: str) -> None:
        """Stop what is playing and drop every sentence still waiting for its turn.

        Sentences requested after this call belong to the next generation and are unaffected, so
        the next turn speaks normally.
        """
        if not self._busy_speaking:
            return
        self._generation += 1
        self._interrupt_reason = reason
        self._barge.disarm()
        self._last_audio_end = None  # an interrupted answer has no gap to measure
        # The look-ahead goes first and without waiting: every sentence that is only waiting is
        # of the old generation now, so its synthesis and buffered audio are of no use to anyone.
        for waiting in self._waiting:
            waiting.audio.cancel_nowait()
        await self._audio_out.stop(reason=reason)
        playing = self._playing
        if playing is not None:
            playing.audio.cancel_nowait()  # the rest of the sentence that was cut off
