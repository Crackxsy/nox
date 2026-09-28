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
keeps the capture device closed until push-to-talk is actually held. A follow-up spoken while the
conversation window is open counts as addressed to Nox.

Third gate: Nox's own voice (`nox.voice.echo`). In continuous listening, speech that starts while
Nox is audible is echo unless it is louder than the playback, and a kill phrase Nox itself just
said is never obeyed.

Speaking: every reply arrives sentence by sentence. A barge-in or a `tts.stop` cancels the whole
turn - the sentence playing, the ones queued behind it and any that arrive for it later - not only
the sentence that happened to be playing.

Two loops run beside the capture loop so the event loop is never the bottleneck: the wake-word
detector's ONNX inference runs in a worker thread over batches of queued frames, and transcription
runs one utterance at a time through a single-consumer queue, which also keeps transcript events in
utterance order. That queue is bounded: under a backlog the oldest unforced utterance is dropped.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from nox.core.events import E, HealthStatus, TranscriptReady, TtsStarted, VoiceKillPhrase
from nox.util.aio import aclose
from nox.voice._logging import get_logger
from nox.voice.audio import AudioUnavailableError
from nox.voice.base import AudioInput, AudioOutput, SttEngine, TtsEngine, TtsRequest
from nox.voice.echo import EchoGuard
from nox.voice.stt.wake_gate import GateDecision, WakeGate
from nox.voice.stt.wake_word import WakeWordMatcher, mentions_kill_phrase
from nox.voice.turns import TurnLedger, turn_of
from nox.voice.vad import EnergyVad, Segmenter, SegmentEvent, SegmentKind

log = get_logger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
CaptureAllowed = Callable[[], bool]

#: An open capture stream delivers a frame every 30 ms; this long without one means the device
#: disappeared or its stream stopped.
CAPTURE_STALL_S = 3.0
#: Utterances waiting for Whisper. More than this is a backlog nobody is waiting for any more.
PENDING_MAX = 4


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
    #: Continuous listening only: while Nox is audible, speech must be louder than the playback
    #: (by `half_duplex_margin_db`) to count as the user; see `nox.voice.echo`.
    half_duplex: bool = True
    half_duplex_margin_db: float = 0.0
    #: Push-to-talk is released on its own after this long, so a missed key-up (a locked screen
    #: while the combo was held) cannot leave the microphone open.
    ptt_max_hold_s: float = Field(default=60.0, gt=0.0)


@dataclass(frozen=True, slots=True)
class _Utterance:
    """One finished segment waiting for its turn on the single transcription slot."""

    audio: np.ndarray
    forced: bool
    duration_ms: int
    started_at: float
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audio_in = audio_in
        self._audio_out = audio_out
        self._stt = stt
        self._tts = tts
        self._emit = emit
        self._capture_allowed = capture_allowed
        self._clock = clock
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
        self._echo = EchoGuard(
            half_duplex=self.config.half_duplex,
            margin_db=self.config.half_duplex_margin_db,
            clock=clock,
        )
        self._muted = False
        self._kill_latched = False
        self._stopped = False
        self._started = False
        self._speaking = False
        self._ptt_active = False
        self._ptt_watchdog: asyncio.Task[None] | None = None
        self._say_lock = asyncio.Lock()
        self._turns = TurnLedger()
        self._listen_task: asyncio.Task[None] | None = None
        self._detect_task: asyncio.Task[None] | None = None
        self._transcribe_task: asyncio.Task[None] | None = None
        self._detect_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._pending: deque[_Utterance] = deque()
        self._pending_ready = asyncio.Event()
        self._segment_started_at = 0.0
        self._segment_is_echo = False
        self._capture_opened_at = 0.0
        self._last_frame_at = 0.0
        #: Why the capture device could not be opened; cleared by the next successful open.
        self._device_error = ""
        #: Empty while capture works, otherwise why it stopped - read by `capture_health()`.
        self.capture_error = ""
        self.frames_seen = 0
        self.utterances = 0
        self.backlog_dropped = 0

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
    def ptt_active(self) -> bool:
        return self._ptt_active

    @property
    def wake_gate(self) -> WakeGate:
        return self._gate

    @property
    def echo(self) -> EchoGuard:
        return self._echo

    @property
    def gated_out(self) -> int:
        """Segments that never became a transcript event; the gate counts them by reason."""
        return self._gate.not_reported

    def wake_gate_health(self) -> tuple[HealthStatus, str]:
        return self._gate.health()

    def capture_health(self) -> tuple[HealthStatus, str]:
        """Whether the capture path works: `unavailable` once the device is gone or silent."""
        if self.capture_error:
            return HealthStatus.UNAVAILABLE, self.capture_error
        if not self._started:
            return HealthStatus.UNAVAILABLE, "capture not started"
        if self._device_error:
            return HealthStatus.UNAVAILABLE, self._device_error
        if self._audio_in.enabled:
            silent_s = self._clock() - max(self._last_frame_at, self._capture_opened_at)
            if silent_s > CAPTURE_STALL_S:
                return HealthStatus.UNAVAILABLE, (
                    f"no audio from the microphone for {silent_s:.0f} s "
                    "(device unplugged or its stream stopped)"
                )
            return HealthStatus.AVAILABLE, "capturing"
        if self.config.listening_mode == "ptt_only" and self.gate_open():
            return HealthStatus.AVAILABLE, "microphone closed until push-to-talk is held"
        return HealthStatus.AVAILABLE, "microphone closed (privacy, mute or safe mode)"

    def health_report(self) -> dict[str, tuple[HealthStatus, str]]:
        """Per component; the wake gate and echo handling only matter in continuous listening."""
        report = {"capture": self.capture_health()}
        if self.config.listening_mode == "continuous":
            report["wake_gate"] = self.wake_gate_health()
            report["echo"] = self._echo.health()
        return report

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
        the device is not merely ignored while PTT is up, it is not open at all. A device that
        cannot be opened is recorded for `capture_health` instead of failing the caller.
        """
        active = self.capture_active()
        was_enabled = self._audio_in.enabled
        try:
            await self._audio_in.set_enabled(active)
        except Exception as exc:  # noqa: BLE001 - reported through capture_health, never hidden
            self._device_error = f"cannot open the microphone: {type(exc).__name__}: {exc}"
            log.error("voice.capture_open_failed", error=self._device_error)
            active = False
        else:
            if active:
                self._device_error = ""
                if not was_enabled:
                    self._capture_opened_at = self._clock()
        if not active and self._segmenter.active:
            self._segmenter.reset()
            self._segment_is_echo = False
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
            half_duplex=self.config.half_duplex,
        )

    async def stop(self) -> None:
        self._stopped = True
        self._started = False
        self._cancel_ptt_watchdog()
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
                self._last_frame_at = self._clock()
                if not self.capture_active():
                    # Defense in depth: even if the device still delivers frames, drop them here.
                    await self._audio_in.set_enabled(False)
                    if self._segmenter.active:
                        self._segmenter.reset()
                        self._segment_is_echo = False
                    continue
                self.frames_seen += 1
                # The detector sees every frame, so a wake word spoken *before* the segment opened
                # still counts; the inference itself happens in `_detect_loop`, off this loop.
                self._detect_queue.put_nowait(frame)
                event = self._segmenter.push(frame)
                if event is not None:
                    await self._on_segment(event, frame)
                elif self._segment_is_echo and self._echo.louder_than_playback(frame):
                    await self._promote_echo_segment()
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

    async def _on_segment(self, event: SegmentEvent, frame: np.ndarray | None = None) -> None:
        if event.kind == SegmentKind.START:
            self._segment_started_at = self._clock()
            if not event.forced and self._echo.in_echo_window():
                if frame is None or not self._echo.louder_than_playback(frame):
                    # Nox's own voice from the speakers, as far as anyone can tell without echo
                    # cancellation: no barge-in, no "listening" indicator, no transcription.
                    self._segment_is_echo = True
                    return
            self._segment_is_echo = False
            await self._emit(E.VOICE_INPUT_STARTED, {"forced": event.forced})
            if self._speaking and self.config.barge_in:
                await self.interrupt(reason="barge_in")
        elif event.kind == SegmentKind.END and event.audio is not None:
            if self._segment_is_echo:
                self._segment_is_echo = False
                self._echo.suppressed += 1
                log.debug("voice.echo_suppressed", duration_ms=event.duration_ms)
                return
            await self._emit(E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms})
            self._enqueue(
                _Utterance(
                    audio=event.audio,
                    forced=event.forced,
                    duration_ms=event.duration_ms,
                    started_at=self._segment_started_at,
                    ended_at=self._clock(),
                )
            )
        elif event.kind == SegmentKind.ABORT:
            if self._segment_is_echo:
                self._segment_is_echo = False
                return
            await self._emit(
                E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms, "dropped": True}
            )

    async def _promote_echo_segment(self) -> None:
        """A segment first taken for echo got louder than the playback: it is the user after all."""
        self._segment_is_echo = False
        await self._emit(E.VOICE_INPUT_STARTED, {"forced": False})
        if self._speaking and self.config.barge_in:
            await self.interrupt(reason="barge_in")

    def _enqueue(self, utterance: _Utterance) -> None:
        """Queue for transcription; under a backlog the oldest unforced utterance gives way."""
        if len(self._pending) >= PENDING_MAX:
            victim = next((u for u in self._pending if not u.forced), None)
            if victim is not None:
                self._pending.remove(victim)
                self.backlog_dropped += 1
                self._gate.note_not_reported("backlog_dropped")
                log.warning("voice.backlog_dropped", pending=len(self._pending))
        self._pending.append(utterance)
        self._pending_ready.set()

    async def _transcribe_loop(self) -> None:
        """One utterance at a time: bounds the CPU Whisper may use and keeps events in order."""
        while True:
            await self._pending_ready.wait()
            if not self._pending:
                self._pending_ready.clear()
                continue
            utterance = self._pending.popleft()
            try:
                await self._process_utterance(utterance)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad utterance must not end the loop
                log.error("voice.utterance_failed", error=f"{type(exc).__name__}: {exc}")

    async def _process_utterance(self, utterance: _Utterance) -> None:
        forced = utterance.forced
        if not self.gate_open() and not forced:
            return
        decision = self._gate.decide(
            duration_ms=utterance.duration_ms, ptt=forced or self._ptt_active
        )
        if decision is GateDecision.DROP:
            # Whisper never sees this audio: no transcript, no event, and no log line carrying any
            # content - only the fact that a segment was dropped (counted by the gate itself).
            log.debug("voice.gated_out", duration_ms=utterance.duration_ms)
            return
        self.utterances += 1
        try:
            transcript = await self._stt.transcribe(
                utterance.audio, self._audio_in.sample_rate, language=self.config.language
            )
        except Exception as exc:  # noqa: BLE001 - engine failure must not kill the listen loop
            log.error("voice.stt_failed", error=f"{type(exc).__name__}: {exc}")
            return
        text = transcript.text.strip()
        if not text:
            return
        match = self._wake.match(text)
        if match.kill and not forced:
            if self._echo.heard_own_kill_phrase(utterance.started_at, utterance.ended_at):
                # Nox just said the phrase itself (a reply quoting it); obeying its own echo would
                # engage the kill switch nobody asked for. Push-to-talk is never second-guessed.
                self._gate.note_not_reported("own_kill_phrase")
                log.warning("voice.kill_phrase_self_trigger_ignored")
                return
        if decision.watchdog_only and not match.kill:
            # The segment only got through so the kill phrase stays reachable without an acoustic
            # model for it; anything else is discarded here and never becomes an event.
            self._gate.note_not_reported("watchdog_discarded")
            log.debug("voice.watchdog_discarded", duration_ms=transcript.duration_ms)
            return
        if match.kill:
            await self._engage_kill(transcript.language)
            return
        addressed = (
            forced or match.addressed or decision in (GateDecision.WAKE, GateDecision.CONVERSATION)
        )
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

    async def _engage_kill(self, language: str) -> None:
        self._kill_latched = True
        await self.refresh_gate()
        await self.interrupt(reason="kill_phrase")
        payload = VoiceKillPhrase(by="voice", language=language).model_dump(mode="json")
        await self._emit(E.VOICE_KILL_PHRASE, payload)
        log.warning("voice.kill_phrase")

    # ---- push-to-talk and mute -------------------------------------------------------------------

    def _closed_reason(self) -> str:
        if self._kill_latched:
            return "safe_mode"
        if self._muted:
            return "muted"
        return "capture_closed"

    async def push_to_talk(self, pressed: bool) -> None:
        if not pressed:
            await self._release_ptt(reason="")
            return
        if not self.gate_open():
            log.info("voice.ptt_refused", reason=self._closed_reason())
            await self._emit(E.VOICE_PTT_REFUSED, {"reason": self._closed_reason()})
            return
        if self._ptt_active:
            return
        self._ptt_active = True
        # In `ptt_only` this is where the capture device is opened, and nowhere earlier.
        await self.refresh_gate()
        if self._device_error:
            self._ptt_active = False
            await self._emit(E.VOICE_PTT_REFUSED, {"reason": "microphone_unavailable"})
            return
        await self._emit(E.VOICE_PTT_PRESSED, {})
        self._ptt_watchdog = asyncio.create_task(self._ptt_max_hold(), name="voice-ptt-max-hold")
        event = self._segmenter.force_start()
        if event is not None:
            await self._on_segment(event)
        elif self._speaking and self.config.barge_in:
            await self.interrupt(reason="barge_in")

    async def _ptt_max_hold(self) -> None:
        await asyncio.sleep(self.config.ptt_max_hold_s)
        log.warning("voice.ptt_auto_released", held_s=self.config.ptt_max_hold_s)
        self._ptt_watchdog = None
        await self._release_ptt(reason="max_hold")

    def _cancel_ptt_watchdog(self) -> None:
        watchdog, self._ptt_watchdog = self._ptt_watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()

    async def _release_ptt(self, *, reason: str) -> None:
        """End push-to-talk. `reason` is empty for a key release, `max_hold` for the watchdog."""
        self._cancel_ptt_watchdog()
        if not self._ptt_active:
            return
        self._ptt_active = False
        await self._emit(E.VOICE_PTT_RELEASED, {"reason": reason} if reason else {})
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

    async def _interrupted(self, request: TtsRequest, reason: str) -> None:
        await self._emit(
            E.TTS_INTERRUPTED, {"utterance_id": request.utterance_id, "reason": reason}
        )

    async def say(self, request: TtsRequest) -> None:
        """Speak one utterance, after the ones queued before it - unless its turn was cancelled."""
        turn = turn_of(request)
        cancelled = self._turns.cancelled(turn)
        if cancelled is not None:
            await self._interrupted(request, cancelled)
            return
        self._turns.enqueue(turn)
        try:
            await self._say_lock.acquire()
        finally:
            self._turns.dequeue(turn)
        try:
            reason = self._turns.cancelled(turn) or ("stopped" if self._stopped else None)
            if reason is not None:
                await self._interrupted(request, reason)
                return
            await self._speak(request, turn)
        finally:
            self._say_lock.release()

    async def _speak(self, request: TtsRequest, turn: str) -> None:
        started = TtsStarted(
            text=request.text,
            channel=str(request.channel),
            engine=self._tts.id,
            utterance_id=request.utterance_id,
        )
        # Set before the await: a barge-in landing while `tts.started` is still in flight must
        # interrupt this utterance, not be dropped as "not speaking yet".
        self._speaking = True
        self._turns.current = turn
        self._echo.playback_started(
            channel=request.channel, mentions_kill=mentions_kill_phrase(request.text)
        )
        failure = ""
        try:
            await self._emit(E.TTS_STARTED, started.model_dump(mode="json"))
            audio = self._echo.metered(self._tts.synthesize(request))
            try:
                await self._audio_out.play(
                    audio, self._tts.sample_rate, request.channel, utterance_id=request.utterance_id
                )
            finally:
                await aclose(audio)
        except asyncio.CancelledError:
            # The worker is shutting down: still no utterance without its terminal event.
            await self._interrupted(request, "cancelled")
            raise
        except (AudioUnavailableError, FileNotFoundError, ValueError) as exc:
            log.error("voice.tts_failed", utterance_id=request.utterance_id, error=str(exc))
            failure = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - reported as tts.finished(ok=False), exactly once
            # Anything else is still an utterance the user did not hear. It is reported here and
            # not re-raised: the worker would otherwise report the same utterance a second time.
            log.exception(
                "voice.tts_failed",
                utterance_id=request.utterance_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            failure = type(exc).__name__
        finally:
            self._speaking = False
            self._turns.current = None
            self._echo.playback_ended()
            if self._gate.conversation_open:
                # The follow-up window counts from the end of the answer, not from the question.
                self._gate.note_addressed()
        await self._finish(request, turn, failure=failure)

    async def _finish(self, request: TtsRequest, turn: str, *, failure: str) -> None:
        """Exactly one terminal event per utterance, so no `say` ever dangles."""
        interrupted = self._turns.cancelled(turn)
        if not failure and interrupted is not None:
            await self._interrupted(request, interrupted)
            return
        payload: dict[str, Any] = {"utterance_id": request.utterance_id, "ok": not failure}
        if failure:
            payload["reason"] = failure
        await self._emit(E.TTS_FINISHED, payload)

    async def interrupt(self, *, reason: str) -> None:
        """Stop speaking and cancel the turns that are playing or queued (barge-in, stop, kill)."""
        self._turns.cancel_active(reason)
        if not self._speaking:
            return
        await self._audio_out.stop(reason=reason)
