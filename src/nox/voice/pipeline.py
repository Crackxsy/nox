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
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from nox.core.events import E, HealthStatus, TranscriptReady, TtsStarted, VoiceKillPhrase
from nox.util.aio import aclose
from nox.voice._logging import get_logger
from nox.voice.audio import AudioUnavailableError
from nox.voice.base import AudioInput, AudioOutput, SttEngine, TtsEngine, TtsRequest
from nox.voice.stt.wake_gate import GateDecision, WakeGate
from nox.voice.stt.wake_word import WakeWordMatcher
from nox.voice.vad import EnergyVad, Segmenter, SegmentEvent, SegmentKind

log = get_logger(__name__)

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


@dataclass(frozen=True, slots=True)
class _Utterance:
    """One finished segment waiting for its turn on the single transcription slot."""

    audio: np.ndarray
    forced: bool
    duration_ms: int


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
    ) -> None:
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
        self._interrupt_reason: str | None = None
        self._say_lock = asyncio.Lock()
        self._listen_task: asyncio.Task[None] | None = None
        self._detect_task: asyncio.Task[None] | None = None
        self._transcribe_task: asyncio.Task[None] | None = None
        self._detect_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._pending: asyncio.Queue[_Utterance] = asyncio.Queue()
        self._current_audio: AsyncIterator[bytes] | None = None
        #: Empty while capture works, otherwise why it stopped - read by `capture_health()`.
        self.capture_error = ""
        self.frames_seen = 0
        self.utterances = 0

    # ---- state -----------------------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._speaking

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
        """Whether the capture path is still alive - `unavailable` once the listen loop has died."""
        if self.capture_error:
            return HealthStatus.UNAVAILABLE, self.capture_error
        if not self._started:
            return HealthStatus.UNAVAILABLE, "capture not started"
        return HealthStatus.AVAILABLE, "capturing"

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
        await self._audio_in.set_enabled(active)
        if not active and self._segmenter.active:
            self._segmenter.reset()
        if not active:
            self._gate.reset()

    async def reset_kill(self) -> None:
        self._kill_latched = False
        await self.refresh_gate()

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._stopped = False
        self._started = True
        self.capture_error = ""
        await self._audio_in.start()
        await self.refresh_gate()
        self._listen_task = asyncio.create_task(self._listen(), name="voice-listen")
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
        await self._audio_in.set_enabled(False)
        await self.interrupt(reason="stop")
        await self._audio_in.stop()
        tasks = [t for t in (self._listen_task, self._detect_task, self._transcribe_task) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._listen_task = self._detect_task = self._transcribe_task = None
        self._segmenter.reset()
        log.info("voice.pipeline_stopped")

    # ---- capture side ----------------------------------------------------------------------------

    async def _listen(self) -> None:
        """Pull frames from the capture device until it ends or fails.

        A failure here would make the pipeline permanently deaf, so it is recorded on
        `capture_error` and reported by `capture_health` instead of ending the task in silence.
        """
        try:
            async for frame in self._audio_in.frames():
                if self._stopped:
                    return
                if not self.capture_active():
                    # Defense in depth: even if the device still delivers frames, drop them here.
                    await self._audio_in.set_enabled(False)
                    if self._segmenter.active:
                        self._segmenter.reset()
                    continue
                self.frames_seen += 1
                # The detector sees every frame, so a wake word spoken *before* the segment opened
                # still counts; the inference itself happens in `_detect_loop`, off this loop.
                self._detect_queue.put_nowait(frame)
                event = self._segmenter.push(frame)
                if event is not None:
                    await self._on_segment(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported through the health check, never hidden
            self.capture_error = f"microphone capture stopped: {type(exc).__name__}: {exc}"
            log.error("voice.capture_failed", error=f"{type(exc).__name__}: {exc}")
            return
        if not self._stopped:
            self.capture_error = "the capture device stopped delivering frames"
            log.error("voice.capture_ended")

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
            if self._speaking and self.config.barge_in:
                await self.interrupt(reason="barge_in")
        elif event.kind == SegmentKind.END and event.audio is not None:
            await self._emit(E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms})
            self._pending.put_nowait(
                _Utterance(audio=event.audio, forced=event.forced, duration_ms=event.duration_ms)
            )
        elif event.kind == SegmentKind.ABORT:
            await self._emit(
                E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms, "dropped": True}
            )

    async def _transcribe_loop(self) -> None:
        """One utterance at a time: bounds the CPU Whisper may use and keeps events in order."""
        while True:
            utterance = await self._pending.get()
            try:
                await self._process_utterance(
                    utterance.audio, forced=utterance.forced, duration_ms=utterance.duration_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad utterance must not end the loop
                log.error("voice.utterance_failed", error=f"{type(exc).__name__}: {exc}")

    async def _process_utterance(
        self, audio: np.ndarray, *, forced: bool, duration_ms: int = 0
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
            elif self._speaking and self.config.barge_in:
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
        async with self._say_lock:
            if self._stopped:
                return
            self._interrupt_reason = None
            started = TtsStarted(
                text=request.text,
                channel=str(request.channel),
                engine=self._tts.id,
                utterance_id=request.utterance_id,
            )
            # Set before the await: a barge-in landing while `tts.started` is still in flight must
            # interrupt this utterance, not be dropped as "not speaking yet".
            self._speaking = True
            await self._emit(E.TTS_STARTED, started.model_dump(mode="json"))
            audio = self._tts.synthesize(request)
            self._current_audio = audio
            reported = False
            try:
                await self._audio_out.play(
                    audio, self._tts.sample_rate, request.channel, utterance_id=request.utterance_id
                )
            except (AudioUnavailableError, FileNotFoundError, ValueError) as exc:
                log.error("voice.tts_failed", utterance_id=request.utterance_id, error=str(exc))
                reported = True
                await self._finish(request, ok=False, reason=type(exc).__name__)
                return
            except Exception as exc:
                # Anything else is still an utterance the user did not hear. Report it, then let
                # the caller see the original error.
                log.error(
                    "voice.tts_failed",
                    utterance_id=request.utterance_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                reported = True
                await self._finish(request, ok=False, reason=type(exc).__name__)
                raise
            finally:
                self._speaking = False
                self._current_audio = None
                await aclose(audio)
                if not reported:
                    await self._finish(request, ok=True, reason="")

    async def _finish(self, request: TtsRequest, *, ok: bool, reason: str) -> None:
        """Exactly one terminal event per utterance, so no `say` ever dangles."""
        if ok and self._interrupt_reason is not None:
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
        if not self._speaking:
            return
        self._interrupt_reason = reason
        await self._audio_out.stop(reason=reason)
