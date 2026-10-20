"""`tts.level`: while Nox speaks, the loudness of what the output device is playing, about 30
times a second - one number, so the pet's mouth can follow the voice without the words."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from nox.core.events import E, validate_payload
from nox.voice.audio import block_level
from nox.voice.base import Channel, TtsRequest
from nox.voice.pipeline import LEVEL_HZ, DefaultVoicePipeline, PipelineConfig
from nox.voice.vad import Segmenter
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt, FakeTts


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        validate_payload(name, payload)
        self.events.append((name, payload))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [p for n, p in self.events if n == name]


class LoudOutput(FakeAudioOutput):
    """Plays each chunk for a few pacer periods at a known loudness."""

    def __init__(self, levels: list[float]) -> None:
        super().__init__()
        self.levels = levels
        self.level = 0.0

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        async for _ in pcm_chunks:
            pass
        for value in self.levels:
            self.level = value
            await asyncio.sleep(3 / LEVEL_HZ)
        self.level = 0.0


def pipeline(out: FakeAudioOutput) -> tuple[DefaultVoicePipeline, Recorder]:
    rec = Recorder()
    p = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=out,
        stt=FakeStt(),
        tts=FakeTts(),
        emit=rec,
        capture_allowed=lambda: True,
        config=PipelineConfig(),
        segmenter=Segmenter(
            sample_rate=16000, end_silence_ms=300, min_segment_ms=200, pre_roll_ms=60
        ),
    )
    return p, rec


async def test_levels_follow_what_is_playing_and_stop_with_the_utterance() -> None:
    out = LoudOutput([0.2, 0.6, 0.3])
    p, rec = pipeline(out)
    await p.say(TtsRequest(utterance_id="u1", text="Hallo.", channel=Channel.STREAM))
    levels = rec.of(E.TTS_LEVEL)
    values = [e["level"] for e in levels]
    assert {0.2, 0.6, 0.3} <= set(values)
    assert all(set(e) == {"utterance_id", "channel", "level"} for e in levels)
    assert {(e["utterance_id"], e["channel"]) for e in levels} == {("u1", "stream")}
    # nothing after the utterance has finished
    names = [n for n, _ in rec.events]
    assert names[-1] == E.TTS_FINISHED
    await asyncio.sleep(5 / LEVEL_HZ)
    assert [n for n, _ in rec.events][-1] == E.TTS_FINISHED


async def test_unchanged_loudness_is_not_resent() -> None:
    out = LoudOutput([0.5] * 4)
    p, rec = pipeline(out)
    await p.say(TtsRequest(utterance_id="u2", text="Hallo.", channel=Channel.BOTH))
    assert [e["level"] for e in rec.of(E.TTS_LEVEL)].count(0.5) == 1


async def test_no_levels_for_a_muted_channel_or_an_output_that_cannot_measure() -> None:
    p, rec = pipeline(LoudOutput([0.5]))
    await p.say(TtsRequest(utterance_id="u3", text="Hallo.", channel=Channel.MUTE))
    assert rec.of(E.TTS_LEVEL) == []
    p, rec = pipeline(FakeAudioOutput())
    await p.say(TtsRequest(utterance_id="u4", text="Hallo.", channel=Channel.BOTH))
    assert rec.of(E.TTS_LEVEL) == []


def test_block_level_is_rms_of_full_scale() -> None:
    assert block_level(np.zeros(256, np.int16), 256) == 0.0
    full = np.full(256, 32767, np.int16)
    assert block_level(full, 256) == pytest.approx(1.0, abs=1e-3)
    half = (np.sin(np.linspace(0, 20 * np.pi, 512)) * 16384).astype(np.int16)
    assert block_level(half, 512) == pytest.approx(0.5 / np.sqrt(2), abs=0.01)
    # a short last block counts the silence that pads it
    assert block_level(full[:128], 256) == pytest.approx(np.sqrt(0.5), abs=1e-3)


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), float("inf")])
def test_payload_rejects_levels_outside_0_1(bad: float) -> None:
    with pytest.raises(ValidationError):
        validate_payload(E.TTS_LEVEL, {"utterance_id": "u", "channel": "stream", "level": bad})
