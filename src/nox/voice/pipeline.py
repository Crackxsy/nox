"""DefaultVoicePipeline: mic -> VAD -> wake word / PTT -> STT -> events; say() -> TTS -> output.

Implements VoicePipeline (voice/base.py) per ADR-009 and FR-5.1/5.2/5.4/5.5. Runs inside the voice
worker; results leave through the injected `emit(name, payload)` callback which the worker maps to
IPC events. Security gate: no frame is processed unless `capture_allowed()` is True, the pipeline is
not muted and no kill phrase has been latched; the kill phrase is reported as `voice.kill_phrase`
and never reaches the LLM path (no transcript event). Raw audio only ever lives in memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from nox.core.events import E, TranscriptReady, TtsStarted, VoiceKillPhrase
from nox.voice._logging import get_logger
from nox.voice.audio import AudioUnavailableError
from nox.voice.base import AudioInput, AudioOutput, SttEngine, TtsEngine, TtsRequest
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
        self._muted = False
        self._kill_latched = False
        self._stopped = False
        self._started = False
        self._speaking = False
        self._ptt_active = False
        self._interrupt_reason: str | None = None
        self._say_lock = asyncio.Lock()
        self._listen_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._current_audio: AsyncIterator[bytes] | None = None
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

    def gate_open(self) -> bool:
        """True when capture is permitted right now (privacy state, mute, kill latch, running)."""
        if self._stopped or self._muted or self._kill_latched:
            return False
        return bool(self._capture_allowed())

    def refresh_gate(self) -> None:
        """Re-evaluate the capture gate (call after privacy/kill state changes)."""
        allowed = self.gate_open()
        self._set_input_enabled(allowed)
        if not allowed and self._segmenter.active:
            self._segmenter.reset()

    def _set_input_enabled(self, value: bool) -> None:
        setter = getattr(self._audio_in, "enabled", None)
        if setter is not None:
            with_attr: Any = self._audio_in
            with_attr.enabled = value

    def reset_kill(self) -> None:
        self._kill_latched = False
        self.refresh_gate()

    # ---- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._stopped = False
        self._started = True
        await self._audio_in.start()
        self.refresh_gate()
        self._listen_task = asyncio.create_task(self._listen(), name="voice-listen")
        log.info("voice.pipeline_started", gate_open=self.gate_open())

    async def stop(self) -> None:
        self._stopped = True
        self._started = False
        self._set_input_enabled(False)
        await self.interrupt(reason="stop")
        await self._audio_in.stop()
        for task in list(self._tasks):
            task.cancel()
        if self._listen_task is not None:
            self._listen_task.cancel()
            await asyncio.gather(self._listen_task, return_exceptions=True)
            self._listen_task = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self._segmenter.reset()
        log.info("voice.pipeline_stopped")

    # ---- capture side ----------------------------------------------------------------------------

    async def _listen(self) -> None:
        async for frame in self._audio_in.frames():
            if self._stopped:
                return
            if not self.gate_open():
                # Defense in depth: even if the input still delivers frames, drop them here.
                self._set_input_enabled(False)
                if self._segmenter.active:
                    self._segmenter.reset()
                continue
            self.frames_seen += 1
            event = self._segmenter.push(frame)
            if event is not None:
                await self._on_segment(event)

    async def _on_segment(self, event: SegmentEvent) -> None:
        if event.kind == SegmentKind.START:
            await self._emit(E.VOICE_INPUT_STARTED, {"forced": event.forced})
            if self._speaking and self.config.barge_in:
                await self.interrupt(reason="barge_in")
        elif event.kind == SegmentKind.END and event.audio is not None:
            await self._emit(E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms})
            self._spawn(self._process_utterance(event.audio, forced=event.forced))
        elif event.kind == SegmentKind.ABORT:
            await self._emit(
                E.VOICE_INPUT_STOPPED, {"duration_ms": event.duration_ms, "dropped": True}
            )

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_utterance(self, audio: np.ndarray, *, forced: bool) -> None:
        if not self.gate_open() and not forced:
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
        if match.kill:
            self._kill_latched = True
            self.refresh_gate()
            await self.interrupt(reason="kill_phrase")
            payload = VoiceKillPhrase(by="voice", language=transcript.language).model_dump(
                mode="json"
            )
            await self._emit(E.VOICE_KILL_PHRASE, payload)
            log.warning("voice.kill_phrase")
            return
        addressed = forced or match.addressed
        if not addressed and self.config.require_wake_word:
            log.debug("voice.unaddressed_dropped", duration_ms=transcript.duration_ms)
            return
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

    async def set_muted(self, muted: bool) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        self.refresh_gate()
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
            await self._emit(E.TTS_STARTED, started.model_dump(mode="json"))
            audio = self._tts.synthesize(request)
            self._current_audio = audio
            self._speaking = True
            try:
                await self._audio_out.play(
                    audio, self._tts.sample_rate, request.channel, utterance_id=request.utterance_id
                )
            except (AudioUnavailableError, FileNotFoundError, ValueError) as exc:
                log.error("voice.tts_failed", utterance_id=request.utterance_id, error=str(exc))
                await self._emit(
                    E.TTS_FINISHED,
                    {
                        "utterance_id": request.utterance_id,
                        "ok": False,
                        "reason": type(exc).__name__,
                    },
                )
                return
            finally:
                self._speaking = False
                self._current_audio = None
                aclose = getattr(audio, "aclose", None)
                if aclose is not None:
                    await aclose()
            if self._interrupt_reason is not None:
                await self._emit(
                    E.TTS_INTERRUPTED,
                    {"utterance_id": request.utterance_id, "reason": self._interrupt_reason},
                )
            else:
                await self._emit(E.TTS_FINISHED, {"utterance_id": request.utterance_id, "ok": True})

    async def interrupt(self, *, reason: str) -> None:
        if not self._speaking:
            return
        self._interrupt_reason = reason
        await self._audio_out.stop(reason=reason)
