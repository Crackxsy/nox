"""Voice keeps its promises: barge-in ends the whole reply, follow-ups reach the orchestrator,
Nox's own voice neither interrupts nor kills it, and a dead microphone is reported (all fakes)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from nox.core.events import E, Event, HealthStatus, validate_payload
from nox.core.orchestrator import Orchestrator, OrchestratorConfig
from nox.voice.base import Channel, TtsRequest
from nox.voice.pipeline import CAPTURE_STALL_S, PENDING_MAX, DefaultVoicePipeline, PipelineConfig
from nox.voice.stt.wake_gate import WakeGate, WakeGateConfig
from nox.voice.vad import Segmenter
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeStt,
    FakeTts,
    frames_of,
    noise,
    settle,
    tone,
)
from tests.unit.voice.test_pipeline_gate import FakeAcousticDetector, FakeClock, wake_frame


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        validate_payload(name, payload)
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [p for n, p in self.events if n == name]


class LoudTts(FakeTts):
    """Plays real-looking PCM at a known level, so the echo guard has something to compare."""

    def __init__(self, *, amplitude: int = 10000, chunks: int = 40, delay: float = 0.01) -> None:
        super().__init__(chunks=chunks, delay=delay)
        self.amplitude = amplitude

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        self.requests.append(request)
        pcm = (np.sin(np.arange(441) / 5.0) * self.amplitude).astype(np.int16).tobytes()
        try:
            for _ in range(self.chunks):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield pcm
        finally:
            self.closed += 1


def make(
    audio_in: FakeAudioInput,
    audio_out: FakeAudioOutput,
    *,
    stt: FakeStt | None = None,
    tts: FakeTts | None = None,
    gate: WakeGate | None = None,
    clock: FakeClock | None = None,
    emit: Any = None,
    **config: Any,
) -> tuple[DefaultVoicePipeline, Recorder]:
    rec = Recorder()
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=stt or FakeStt(),
        tts=tts or FakeTts(),
        emit=emit or rec,
        capture_allowed=lambda: True,
        config=PipelineConfig(**config),
        segmenter=Segmenter(
            sample_rate=16000, end_silence_ms=300, min_segment_ms=200, pre_roll_ms=60
        ),
        wake_gate=gate,
        **kwargs,
    )
    return pipeline, rec


def speech(amp: float = 0.3, ms: int = 600) -> list[np.ndarray]:
    return frames_of(np.concatenate([noise(300), tone(ms, amp=amp), noise(500)]))


def sentence(turn: str, index: int, text: str = "Satz.") -> TtsRequest:
    return TtsRequest(utterance_id=f"{turn}:{index}", turn_id=turn, text=text)


# ---- barge-in cancels the whole turn --------------------------------------------------------


async def test_barge_in_cancels_the_queued_sentences_of_the_reply(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, tts=FakeTts(chunks=10, delay=0.01))
    await p.start()
    says = [asyncio.create_task(p.say(sentence("t1", i))) for i in range(4)]
    await asyncio.sleep(0.03)
    assert p.speaking
    await p.push_to_talk(True)  # the user barges in
    await asyncio.gather(*says)
    await p.push_to_talk(False)
    played = {uid: chunks for uid, _, chunks in audio_out.played}
    assert list(played) == ["t1:0"]  # s1..s3 never reached the output
    assert len(played["t1:0"]) < 10
    interrupted = [e["utterance_id"] for e in rec.of(E.TTS_INTERRUPTED)]
    assert interrupted == ["t1:0", "t1:1", "t1:2", "t1:3"]
    assert rec.of(E.TTS_FINISHED) == []
    await p.stop()


async def test_a_stop_while_sentences_are_queued_cancels_them(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, tts=FakeTts(chunks=5, delay=0.01))
    await p.start()
    says = [asyncio.create_task(p.say(sentence("t1", i))) for i in range(3)]
    await asyncio.sleep(0)
    await p.interrupt(reason="new_input")  # the orchestrator's `tts.stop`
    await asyncio.gather(*says)
    assert all(e["reason"] == "new_input" for e in rec.of(E.TTS_INTERRUPTED))
    assert len(rec.of(E.TTS_INTERRUPTED)) == 3
    await p.stop()


async def test_late_sentences_of_a_cancelled_turn_are_dropped_but_a_new_turn_plays(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, tts=FakeTts(chunks=10, delay=0.01))
    await p.start()
    first = asyncio.create_task(p.say(sentence("t1", 0)))
    await asyncio.sleep(0.03)
    await p.interrupt(reason="barge_in")
    await first
    await p.say(sentence("t1", 1))  # the LLM was still streaming the old answer
    await p.say(sentence("t2", 0, "Neue Antwort."))
    assert [uid for uid, _, _ in audio_out.played] == ["t1:0", "t2:0"]
    assert rec.of(E.TTS_FINISHED) == [{"utterance_id": "t2:0", "ok": True}]
    await p.stop()


async def test_orchestrator_sentences_share_the_turn_of_their_reply() -> None:
    bus, speaker = FakeBus(), FakeSpeaker()
    orch = Orchestrator(
        safe_mode=lambda: False,
        bus=bus,
        state=FakeState(),
        router=FakeRouter(bus),
        speaker=speaker,
        turns=FakeTurns(),
        memory_policy=None,
        system_prompt=lambda: "You are Nox.",
        config=OrchestratorConfig(),
    )
    await orch.start()
    turn = await orch.handle_text("Erklär mir kurz Python")
    assert len(speaker.said) > 1
    assert {s.turn_id for s in speaker.said} == {turn.request_id}
    await orch.stop()


# ---- conversation window --------------------------------------------------------------------


async def test_a_follow_up_in_the_conversation_window_is_addressed(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    clock = FakeClock()
    gate = WakeGate(
        detector=FakeAcousticDetector(),
        config=WakeGateConfig(wake_window_s=8.0, conversation_window_s=60.0),
        clock=clock,
    )
    p, rec = make(audio_in, audio_out, stt=FakeStt("und morgen"), gate=gate)
    await p.start()
    audio_in.push(wake_frame())
    await settle(5)
    audio_in.push_all(speech(ms=3000))
    await settle(60)
    clock.advance(30.0)  # wake window over, conversation window open
    audio_in.push_all(speech(ms=3000))
    await settle(60)
    flags = [e["addressed_to_nox"] for e in rec.of(E.VOICE_TRANSCRIPT_READY)]
    assert flags == [True, True]
    await p.stop()


async def test_text_fallback_follow_up_reaches_the_orchestrator_as_a_turn(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """End to end: worker-side pipeline -> event bus -> orchestrator -> language model."""
    bus = FakeBus()
    router = FakeRouter(bus)
    orch = Orchestrator(
        safe_mode=lambda: False,
        bus=bus,
        state=FakeState(),
        router=router,
        speaker=FakeSpeaker(),
        turns=FakeTurns(),
        memory_policy=None,
        system_prompt=lambda: "You are Nox.",
        config=OrchestratorConfig(),
    )
    await orch.start()

    async def to_bus(name: str, payload: dict[str, Any]) -> None:
        validate_payload(name, payload)
        await bus.publish(Event(name=name, payload=payload))

    stt = FakeStt("Nox, wie wird das Wetter")
    p, _ = make(audio_in, audio_out, stt=stt, emit=to_bus)
    await p.start()
    audio_in.push_all(speech())
    await settle(60)
    assert orch.voice_turn is not None
    await orch.voice_turn
    stt.text = "und morgen"  # no wake word: a follow-up
    audio_in.push_all(speech())
    await settle(60)
    await orch.voice_turn
    asked = [r.messages[-1].content for r in router.requests]
    assert asked == ["wie wird das wetter", "und morgen"]
    await p.stop()
    await orch.stop()


async def test_without_a_conversation_speech_is_still_not_addressed(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(
        audio_in,
        audio_out,
        stt=FakeStt("und morgen"),
        gate=WakeGate(config=WakeGateConfig(conversation_window_s=0.0)),
    )
    await p.start()
    audio_in.push_all(speech())
    await settle(60)
    assert [e["addressed_to_nox"] for e in rec.of(E.VOICE_TRANSCRIPT_READY)] == [False]
    await p.stop()


# ---- Nox's own voice ------------------------------------------------------------------------


async def _speaking(
    p: DefaultVoicePipeline, text: str = "Eine lange Antwort."
) -> asyncio.Task[None]:
    task = asyncio.create_task(p.say(TtsRequest(utterance_id="r:0", turn_id="r", text=text)))
    await asyncio.sleep(0.03)
    assert p.speaking
    return task


async def test_half_duplex_ignores_quiet_speech_while_nox_talks(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    stt = FakeStt("Nox wie spät ist es")
    p, rec = make(audio_in, audio_out, stt=stt, tts=LoudTts())
    await p.start()
    say = await _speaking(p)
    audio_in.push_all(speech(amp=0.05))  # the echo of Nox's reply from the speakers
    await settle(60)
    await say
    assert audio_out.stops == []  # no self barge-in
    assert stt.calls == []  # not even transcribed
    assert p.echo.suppressed == 1
    assert rec.of(E.TTS_FINISHED) == [{"utterance_id": "r:0", "ok": True}]
    await p.stop()


async def test_half_duplex_lets_speech_louder_than_the_playback_barge_in(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, tts=LoudTts())
    await p.start()
    say = await _speaking(p)
    audio_in.push_all(speech(amp=0.9))
    await settle(60)
    await say
    assert audio_out.stops == ["barge_in"]
    assert rec.of(E.TTS_INTERRUPTED)[0]["reason"] == "barge_in"
    await p.stop()


async def test_push_to_talk_always_barges_in_during_half_duplex(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, _ = make(audio_in, audio_out, tts=LoudTts())
    await p.start()
    say = await _speaking(p)
    await p.push_to_talk(True)
    await say
    await p.push_to_talk(False)
    assert audio_out.stops == ["barge_in"]
    await p.stop()


async def test_without_half_duplex_quiet_echo_interrupts_and_health_says_so(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, _ = make(audio_in, audio_out, tts=LoudTts(), half_duplex=False)
    await p.start()
    say = await _speaking(p)
    audio_in.push_all(speech(amp=0.05))
    await settle(60)
    await say
    assert audio_out.stops == ["barge_in"]
    status, reason = p.health_report()["echo"]
    assert status is HealthStatus.LIMITED and "no echo cancellation" in reason
    await p.stop()


async def test_half_duplex_is_reported_as_a_limited_reason_in_continuous_mode(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, _ = make(audio_in, audio_out)
    status, reason = p.health_report()["echo"]
    assert status is HealthStatus.LIMITED
    assert "half-duplex (no echo cancellation)" in reason
    q, _ = make(FakeAudioInput(), audio_out, listening_mode="ptt_only")
    assert "echo" not in q.health_report()


async def test_nox_quoting_the_kill_phrase_does_not_kill_it(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """Loud speakers, half-duplex off: the echo reaches Whisper and reads as "Nox Notaus"."""
    clock = FakeClock()
    p, rec = make(
        audio_in,
        audio_out,
        stt=FakeStt("Nox Notaus"),
        tts=LoudTts(chunks=10),
        clock=clock,
        half_duplex=False,
        barge_in=False,
    )
    await p.start()
    say = await _speaking(p, "Sag einfach Nox Notaus, dann halte ich an.")
    audio_in.push_all(speech())
    await settle(60)
    await say
    assert E.VOICE_KILL_PHRASE not in rec.names()
    assert not p.kill_latched
    clock.advance(10.0)  # long after Nox stopped talking, the user says it
    audio_in.push_all(speech())
    await settle(60)
    assert E.VOICE_KILL_PHRASE in rec.names()
    assert p.kill_latched
    await p.stop()


async def test_the_kill_phrase_on_push_to_talk_is_obeyed_even_while_nox_quotes_it(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, stt=FakeStt("Nox Notaus"), tts=LoudTts(chunks=10))
    await p.start()
    say = await _speaking(p, "Sag einfach Nox Notaus.")
    await p.push_to_talk(True)
    audio_in.push_all(speech())
    await settle(10)
    await p.push_to_talk(False)
    await settle(60)
    await say
    assert E.VOICE_KILL_PHRASE in rec.names()
    await p.stop()


# ---- capture health -------------------------------------------------------------------------


async def test_a_microphone_that_stops_delivering_frames_is_unavailable(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    clock = FakeClock()
    p, _ = make(audio_in, audio_out, clock=clock)
    await p.start()
    audio_in.push_all(speech())
    await settle(30)
    assert p.capture_health() == (HealthStatus.AVAILABLE, "capturing")
    clock.advance(CAPTURE_STALL_S + 1)  # the device was unplugged: no frame since
    status, reason = p.health_report()["capture"]
    assert status is HealthStatus.UNAVAILABLE
    assert "no audio from the microphone" in reason
    audio_in.push_all(speech())
    await settle(30)
    assert p.capture_health()[0] is HealthStatus.AVAILABLE
    await p.stop()


async def test_a_microphone_that_cannot_open_refuses_push_to_talk_and_says_why(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    async def unplugged(value: bool) -> None:
        if value:
            raise RuntimeError("no input device")

    audio_in.set_enabled = unplugged  # type: ignore[method-assign]
    p, rec = make(audio_in, audio_out, listening_mode="ptt_only")
    await p.start()
    await p.push_to_talk(True)
    assert rec.of(E.VOICE_PTT_REFUSED) == [{"reason": "microphone_unavailable"}]
    assert E.VOICE_PTT_PRESSED not in rec.names()
    status, reason = p.capture_health()
    assert status is HealthStatus.UNAVAILABLE and "cannot open the microphone" in reason
    await p.stop()


async def test_ptt_only_idle_is_healthy_not_a_dead_microphone(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    clock = FakeClock()
    p, _ = make(audio_in, audio_out, clock=clock, listening_mode="ptt_only")
    await p.start()
    clock.advance(600)
    assert p.capture_health()[0] is HealthStatus.AVAILABLE
    await p.stop()


async def test_ptt_refused_while_muted_is_announced(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, listening_mode="ptt_only")
    await p.start()
    await p.set_muted(True)
    await p.push_to_talk(True)
    assert rec.of(E.VOICE_PTT_REFUSED) == [{"reason": "muted"}]
    await p.stop()


# ---- stuck push-to-talk and backlog ---------------------------------------------------------


async def test_a_push_to_talk_nobody_releases_is_released_after_the_max_hold(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, listening_mode="ptt_only", ptt_max_hold_s=0.05)
    await p.start()
    await p.push_to_talk(True)
    assert audio_in.enabled
    await asyncio.sleep(0.1)  # the key-up was lost (screen locked while holding the combo)
    await settle()
    assert not p.ptt_active
    assert audio_in.enabled is False
    assert rec.of(E.VOICE_PTT_RELEASED) == [{"reason": "max_hold"}]
    await p.push_to_talk(False)  # the late key-up changes nothing
    assert rec.of(E.VOICE_PTT_RELEASED) == [{"reason": "max_hold"}]
    await p.stop()


async def test_a_normal_release_disarms_the_max_hold(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec = make(audio_in, audio_out, listening_mode="ptt_only", ptt_max_hold_s=0.05)
    await p.start()
    await p.push_to_talk(True)
    await p.push_to_talk(False)
    await asyncio.sleep(0.1)
    assert rec.of(E.VOICE_PTT_RELEASED) == [{}]
    await p.stop()


async def test_the_transcription_backlog_is_bounded(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    stt = FakeStt("hintergrund")
    stt.delay = 0.2
    p, _ = make(audio_in, audio_out, stt=stt)
    await p.start()
    for _ in range(PENDING_MAX + 4):
        audio_in.push_all(speech())
        await settle(40)
    assert p.backlog_dropped >= 3
    await p.stop()


async def test_mute_channel_playback_is_not_echo(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """Audio sent only to the stream cable never reaches the room, so it cannot be echo."""
    p, _ = make(audio_in, audio_out, tts=LoudTts())
    await p.start()
    say = asyncio.create_task(
        p.say(TtsRequest(utterance_id="s:0", text="Stream.", channel=Channel.STREAM))
    )
    await asyncio.sleep(0.03)
    audio_in.push_all(speech(amp=0.05))
    await settle(60)
    await say
    assert audio_out.stops == ["barge_in"]
    await p.stop()
