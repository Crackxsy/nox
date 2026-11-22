"""A callout names a prepared clip and carries its words. When the clip is not installed the
pipeline speaks the words in the active voice instead of failing (nothing here plays real audio)."""

from __future__ import annotations

import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nox.core.events import E
from nox.voice.base import Channel, TtsRequest
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.tts.clips import play_clip
from nox.voice.vad import Segmenter
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt
from tests.unit.voice.test_pipeline import Recorder

RATE = 22050


class ClipTts:
    """An engine shaped like the real ones: a prepared clip is read from `clips_dir` (and
    raises `ClipNotFoundError` when it is not there), anything else is "synthesised"."""

    id = "clip-tts"
    sample_rate = RATE

    def __init__(self, clips_dir: Path) -> None:
        self.clips_dir = clips_dir
        self.requests: list[TtsRequest] = []
        self.closed = 0

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        self.requests.append(request)
        try:
            if request.prepared_clip:
                async for chunk in play_clip(request.prepared_clip, self.clips_dir, RATE):
                    yield chunk
                return
            yield f"spoken:{request.text}".encode()
        finally:
            self.closed += 1

    async def health(self) -> tuple[object, str]:  # pragma: no cover - unused by the pipeline
        raise NotImplementedError

    async def load(self) -> None:  # pragma: no cover
        return None

    async def unload(self) -> None:  # pragma: no cover
        return None


def _pipeline(tts: ClipTts) -> tuple[DefaultVoicePipeline, Recorder, FakeAudioOutput]:
    rec, out = Recorder(), FakeAudioOutput()
    pipeline = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=out,
        stt=FakeStt(),
        tts=tts,  # type: ignore[arg-type]
        emit=rec,
        capture_allowed=lambda: True,
        config=PipelineConfig(
            edge_fade_ms=0, trim_silence=False
        ),  # the fake engine's bytes are labels, not audio
        segmenter=Segmenter(sample_rate=16000, end_silence_ms=300, min_segment_ms=200),
    )
    return pipeline, rec, out


def _write_clip(directory: Path, clip_id: str) -> None:
    with wave.open(str(directory / f"{clip_id}.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(b"\x01\x00" * (RATE // 5))


def _callout(text: str = "Careful, demo.") -> TtsRequest:
    return TtsRequest(
        utterance_id="c1",
        text=text,
        language="en",
        channel=Channel.PRIVATE,
        prepared_clip="careful_demo",
    )


async def test_a_missing_clip_is_spoken_from_its_text(tmp_path: Path) -> None:
    tts = ClipTts(tmp_path)
    pipeline, rec, out = _pipeline(tts)
    await pipeline.say(_callout())
    assert [r.prepared_clip for r in tts.requests] == ["careful_demo", None]
    assert tts.requests[1].text == "Careful, demo."
    assert tts.requests[1].language == "en"
    assert out.played[0][2] == [b"spoken:Careful, demo."]
    assert rec.last(E.TTS_FINISHED) == {"utterance_id": "c1", "ok": True}
    assert tts.closed == 2


async def test_an_installed_clip_is_played_and_nothing_is_synthesised(tmp_path: Path) -> None:
    _write_clip(tmp_path, "careful_demo")
    tts = ClipTts(tmp_path)
    pipeline, rec, out = _pipeline(tts)
    await pipeline.say(_callout())
    assert [r.prepared_clip for r in tts.requests] == ["careful_demo"]
    assert out.played[0][2], "the clip's audio reached the output"
    assert not any(chunk.startswith(b"spoken:") for chunk in out.played[0][2])
    assert rec.last(E.TTS_FINISHED) == {"utterance_id": "c1", "ok": True}


async def test_a_missing_clip_without_words_still_fails_loudly(tmp_path: Path) -> None:
    tts = ClipTts(tmp_path)
    pipeline, rec, _ = _pipeline(tts)
    await pipeline.say(_callout(text="  "))
    assert [r.prepared_clip for r in tts.requests] == ["careful_demo"]
    finished = rec.last(E.TTS_FINISHED)
    assert finished["ok"] is False and finished["reason"] == "ClipNotFoundError"


async def test_an_invalid_clip_id_is_not_rescued_by_the_text(tmp_path: Path) -> None:
    tts = ClipTts(tmp_path)
    pipeline, rec, _ = _pipeline(tts)
    bad = _callout().model_copy(update={"prepared_clip": "../secret"})
    await pipeline.say(bad)
    assert rec.last(E.TTS_FINISHED)["ok"] is False
    assert [r.prepared_clip for r in tts.requests] == ["../secret"]


@pytest.mark.parametrize("language", ["de", "en"])
async def test_plain_speech_is_untouched(tmp_path: Path, language: str) -> None:
    tts = ClipTts(tmp_path)
    pipeline, _, out = _pipeline(tts)
    await pipeline.say(TtsRequest(utterance_id="p1", text="Hallo.", language=language))
    assert len(tts.requests) == 1
    assert out.played[0][2] == [b"spoken:Hallo."]
