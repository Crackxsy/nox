"""DefaultVoicePipeline gating, wake word, kill phrase, PTT, barge-in, TTS event order (fakes)."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from nox.core.events import E, validate_payload
from nox.voice.base import Channel, TtsRequest
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.vad import Segmenter
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


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        validate_payload(name, payload)
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def last(self, name: str) -> dict[str, Any]:
        return [p for n, p in self.events if n == name][-1]


def make_pipeline(
    audio_in: FakeAudioInput,
    audio_out: FakeAudioOutput,
    *,
    stt: FakeStt | None = None,
    tts: FakeTts | None = None,
    capture: bool = True,
    **config: Any,
) -> tuple[DefaultVoicePipeline, Recorder, dict[str, bool]]:
    rec = Recorder()
    gate = {"allowed": capture}
    seg = Segmenter(sample_rate=16000, end_silence_ms=300, min_segment_ms=200, pre_roll_ms=60)
    p = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=stt or FakeStt(),
        tts=tts or FakeTts(),
        emit=rec,
        capture_allowed=lambda: gate["allowed"],
        config=PipelineConfig(**config),
        segmenter=seg,
    )
    return p, rec, gate


def utterance() -> list[np.ndarray]:
    return frames_of(np.concatenate([noise(300), tone(600), noise(500)]))


async def test_capture_allowed_false_no_frames(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, _ = make_pipeline(audio_in, audio_out, capture=False)
    await p.start()
    assert audio_in.enabled is False
    audio_in.push_all(utterance())  # dropped at the input (enabled False)
    audio_in.push_all(utterance(), force=True)  # even if frames leak in, the pipeline drops them
    await settle()
    assert p.frames_seen == 0
    assert rec.events == []
    await p.stop()
    assert audio_in.stopped


async def test_wake_word_transcript_addressed(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, _ = make_pipeline(audio_in, audio_out)
    await p.start()
    assert audio_in.enabled is True
    audio_in.push_all(utterance())
    await settle(50)
    names = rec.names()
    assert names[:2] == [E.VOICE_INPUT_STARTED, E.VOICE_INPUT_STOPPED]
    assert names[-1] == E.VOICE_TRANSCRIPT_READY
    ready = rec.last(E.VOICE_TRANSCRIPT_READY)
    assert ready["addressed_to_nox"] is True
    assert ready["text"] == "wie spät ist es"
    assert ready["language"] == "de"
    await p.stop()


async def test_unaddressed_transcript_flagged(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, _ = make_pipeline(audio_in, audio_out, stt=FakeStt("Das war ein guter Save"))
    await p.start()
    audio_in.push_all(utterance())
    await settle(50)
    ready = rec.last(E.VOICE_TRANSCRIPT_READY)
    assert ready["addressed_to_nox"] is False
    assert ready["text"] == "Das war ein guter Save"
    await p.stop()


async def test_require_wake_word_drops_unaddressed(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, _ = make_pipeline(
        audio_in, audio_out, stt=FakeStt("Das war ein guter Save"), require_wake_word=True
    )
    await p.start()
    audio_in.push_all(utterance())
    await settle(50)
    assert E.VOICE_TRANSCRIPT_READY not in rec.names()
    await p.stop()


async def test_kill_phrase_stops_and_latches(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    tts = FakeTts(chunks=50, delay=0.01)
    p, rec, _ = make_pipeline(audio_in, audio_out, stt=FakeStt("Nox Notaus"), tts=tts)
    await p.start()
    say = asyncio.create_task(p.say(TtsRequest(utterance_id="u1", text="Lange Rede.")))
    await asyncio.sleep(0.03)
    assert p.speaking
    audio_in.push_all(utterance())
    await settle(50)
    await say
    names = rec.names()
    assert E.VOICE_KILL_PHRASE in names
    assert E.VOICE_TRANSCRIPT_READY not in names  # never forwarded to the LLM path
    assert E.TTS_INTERRUPTED in names
    assert rec.last(E.TTS_INTERRUPTED)["reason"] == "kill_phrase"
    assert p.kill_latched and not p.gate_open() and audio_in.enabled is False
    # Latched: further speech is ignored until the core resets the worker.
    audio_in.push_all(utterance(), force=True)
    await settle(50)
    assert names == rec.names()
    await p.push_to_talk(True)
    assert E.VOICE_PTT_PRESSED not in rec.names()
    await p.stop()


async def test_push_to_talk_forced_segment(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, _ = make_pipeline(audio_in, audio_out, stt=FakeStt("wie spät ist es"))
    await p.start()
    await p.push_to_talk(True)
    audio_in.push_all(
        frames_of(np.zeros(16000, dtype=np.float32))
    )  # silence: VAD alone would not segment
    await settle()
    await p.push_to_talk(False)
    await settle(50)
    names = rec.names()
    assert names[0] == E.VOICE_PTT_PRESSED
    assert E.VOICE_INPUT_STARTED in names and E.VOICE_PTT_RELEASED in names
    ready = rec.last(E.VOICE_TRANSCRIPT_READY)
    assert ready["addressed_to_nox"] is True and ready["text"] == "wie spät ist es"
    assert 990 <= ready["duration_ms"] <= 1010
    await p.stop()


async def test_mute_gates_input(audio_in: FakeAudioInput, audio_out: FakeAudioOutput) -> None:
    p, rec, _ = make_pipeline(audio_in, audio_out)
    await p.start()
    await p.set_muted(True)
    assert rec.last(E.VOICE_MUTED) == {"muted": True}
    assert audio_in.enabled is False
    audio_in.push_all(utterance(), force=True)
    await settle()
    assert p.frames_seen == 0
    await p.set_muted(False)
    assert audio_in.enabled is True
    await p.stop()


async def test_privacy_change_refresh_gate(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, _, gate = make_pipeline(audio_in, audio_out)
    await p.start()
    gate["allowed"] = False
    await p.refresh_gate()
    assert audio_in.enabled is False
    gate["allowed"] = True
    await p.refresh_gate()
    assert audio_in.enabled is True
    await p.stop()


async def test_say_streams_in_order_and_finishes(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    tts = FakeTts(chunks=4)
    p, rec, _ = make_pipeline(audio_in, audio_out, tts=tts)
    await p.say(TtsRequest(utterance_id="u1", text="Hallo. Ich bin Nox.", channel=Channel.BOTH))
    assert rec.names() == [E.TTS_STARTED, E.TTS_FINISHED]
    started = rec.last(E.TTS_STARTED)
    assert started == {
        "text": "Hallo. Ich bin Nox.",
        "channel": "both",
        "engine": "fake-tts",
        "utterance_id": "u1",
    }
    assert rec.last(E.TTS_FINISHED) == {"utterance_id": "u1", "ok": True}
    uid, channel, chunks = audio_out.played[0]
    assert (uid, channel) == ("u1", Channel.BOTH)
    assert chunks == [bytes([i]) * 4 for i in range(4)]
    assert tts.closed == 1


async def test_barge_in_interrupts_tts(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    tts = FakeTts(chunks=100, delay=0.01)
    p, rec, _ = make_pipeline(audio_in, audio_out, tts=tts, stt=FakeStt("Nox stopp"))
    await p.start()
    say = asyncio.create_task(p.say(TtsRequest(utterance_id="u2", text="Sehr lange Rede.")))
    await asyncio.sleep(0.03)
    audio_in.push_all(utterance())
    await settle(50)
    await say
    assert audio_out.stops == ["barge_in"]
    assert rec.last(E.TTS_INTERRUPTED) == {"utterance_id": "u2", "reason": "barge_in"}
    assert len(audio_out.played[0][2]) < 100
    await p.stop()


async def test_barge_in_disabled(audio_in: FakeAudioInput, audio_out: FakeAudioOutput) -> None:
    tts = FakeTts(chunks=10, delay=0.005)
    p, rec, _ = make_pipeline(audio_in, audio_out, tts=tts, barge_in=False)
    await p.start()
    say = asyncio.create_task(p.say(TtsRequest(utterance_id="u3", text="Rede.")))
    await asyncio.sleep(0.01)
    audio_in.push_all(utterance())
    await say
    assert audio_out.stops == []
    assert E.TTS_FINISHED in rec.names()
    await p.stop()


async def test_stop_interrupts_everything(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    tts = FakeTts(chunks=100, delay=0.01)
    p, rec, _ = make_pipeline(audio_in, audio_out, tts=tts)
    await p.start()
    say = asyncio.create_task(p.say(TtsRequest(utterance_id="u4", text="Rede.")))
    await asyncio.sleep(0.03)
    await p.stop()
    await say
    assert "stop" in audio_out.stops
    assert audio_in.stopped and audio_in.enabled is False
    assert rec.last(E.TTS_INTERRUPTED)["reason"] == "stop"
    # After stop, say() is a no-op and nothing is emitted.
    before = len(rec.events)
    await p.say(TtsRequest(utterance_id="u5", text="Nachzügler."))
    assert len(rec.events) == before


async def test_audio_unavailable_reports_not_speaking(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    from nox.voice.audio import AudioUnavailableError

    class BrokenOut(FakeAudioOutput):
        async def play(
            self, pcm_chunks: Any, sample_rate: int, channel: Channel, *, utterance_id: str
        ) -> None:
            raise AudioUnavailableError("no stream device")

    p, rec, _ = make_pipeline(audio_in, BrokenOut())
    await p.say(TtsRequest(utterance_id="u6", text="Hallo.", channel=Channel.STREAM))
    assert rec.names() == [E.TTS_STARTED, E.TTS_FINISHED]
    assert rec.last(E.TTS_FINISHED)["ok"] is False


async def test_stt_failure_does_not_kill_listen_loop(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    class FailingStt(FakeStt):
        async def transcribe(self, audio: Any, sample_rate: int, *, language: str = "auto") -> Any:
            raise RuntimeError("boom")

    p, rec, _ = make_pipeline(audio_in, audio_out, stt=FailingStt())
    await p.start()
    audio_in.push_all(utterance())
    await settle(50)
    audio_in.push_all(utterance())
    await settle(50)
    assert rec.names().count(E.VOICE_INPUT_STARTED) == 2
    assert E.VOICE_TRANSCRIPT_READY not in rec.names()
    await p.stop()


@pytest.mark.parametrize("bad", ["../secret", "a/b", "x\\y", "UPPER", ""])
async def test_prepared_clip_id_validation(bad: str, tmp_path: Any) -> None:
    from nox.voice.tts.piper_engine import PiperTts

    engine = PiperTts(models_dir=str(tmp_path), clips_dir=str(tmp_path))
    req = TtsRequest(utterance_id="c", text="", prepared_clip=bad or "missing")
    with pytest.raises((ValueError, FileNotFoundError)):
        async for _ in engine.synthesize(req):
            pass
