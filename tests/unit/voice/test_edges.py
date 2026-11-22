"""Soft sentence edges: measured, not assumed. The first and last samples are silence, nothing in
between is touched, and the result does not depend on how the engine cut the audio into chunks."""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np
import pytest

from nox.voice.edges import MAX_FADE_MS, fade_edges, fade_samples

RATE = 24000
LEVEL = 10000


def pcm(samples: np.ndarray) -> bytes:
    return samples.astype("<i2").tobytes()


async def stream(data: bytes, size: int) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def faded(data: bytes, *, size: int = 480, ms: float = 4.0) -> np.ndarray:
    out = b"".join([c async for c in fade_edges(stream(data, size), RATE, ms)])
    return np.frombuffer(out, dtype="<i2")


@pytest.mark.parametrize(
    ("ms", "samples"), [(0.0, 0), (4.0, 96), (10.0, 240), (MAX_FADE_MS, 480), (500.0, 480)]
)
def test_the_fade_length_follows_the_rate_and_is_capped(ms: float, samples: int) -> None:
    assert fade_samples(RATE, ms) == samples


async def test_a_sentence_starts_and_ends_in_silence() -> None:
    out = await faded(pcm(np.full(RATE // 2, LEVEL)))

    assert out[0] == 0 and out[-1] == 0
    assert out.size == RATE // 2  # not a sample added or lost


async def test_the_middle_of_a_sentence_is_untouched() -> None:
    audio = (np.sin(np.arange(RATE) / 7.0) * LEVEL).astype(np.int16)

    out = await faded(pcm(audio))

    n = fade_samples(RATE, 4.0)
    assert np.array_equal(out[n:-n], audio[n:-n])


async def test_the_step_into_and_out_of_silence_is_small() -> None:
    """The click is the largest jump between two neighbouring samples."""
    audio = np.full(RATE // 4, LEVEL, dtype=np.int16)
    hard = np.concatenate([[0], audio, [0]])  # what a stream that is simply cut off does
    soft = np.concatenate([[0], await faded(pcm(audio)), [0]])

    assert int(np.abs(np.diff(hard)).max()) == LEVEL
    assert int(np.abs(np.diff(soft)).max()) <= LEVEL // fade_samples(RATE, 4.0) + 1


@pytest.mark.parametrize("size", [2, 6, 10, 192, 191 * 2, 480, 100_000])
async def test_the_result_does_not_depend_on_how_the_audio_was_chunked(size: int) -> None:
    audio = (np.cos(np.arange(5000) / 11.0) * LEVEL).astype(np.int16)

    assert np.array_equal(await faded(pcm(audio), size=size), await faded(pcm(audio), size=480))


async def test_a_sentence_shorter_than_the_fade_still_starts_and_ends_in_silence() -> None:
    out = await faded(pcm(np.full(40, LEVEL)), ms=4.0)  # 96 samples of fade, 40 of audio

    assert out.size == 40 and out[0] == 0 and out[-1] == 0


async def test_without_a_fade_the_bytes_pass_through_untouched() -> None:
    data = bytes(range(200))

    out = b"".join([c async for c in fade_edges(stream(data, 30), RATE, 0.0)])

    assert out == data


async def test_a_stray_odd_byte_is_carried_not_dropped() -> None:
    data = pcm(np.full(1000, LEVEL)) + b"\x07"

    out = b"".join([c async for c in fade_edges(stream(data, 99), RATE, 4.0)])

    assert len(out) == len(data) and out.endswith(b"\x07")


async def test_an_empty_stream_gives_nothing() -> None:
    assert [c async for c in fade_edges(stream(b"", 10), RATE, 4.0)] == []


async def test_the_tail_is_only_held_back_by_the_length_of_the_fade() -> None:
    """Chunks leave as soon as more than the fade's worth of audio is waiting behind them."""
    released: list[int] = []
    n = fade_samples(RATE, 4.0)
    chunk = pcm(np.full(1000, LEVEL))

    async def source() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield chunk

    async for out in fade_edges(source(), RATE, 4.0):
        released.append(len(out) // 2)

    assert released[0] == 1000 - n  # the first chunk went on, less the part held for the end
    assert sum(released) == 3000
