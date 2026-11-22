"""Soft edges for a sentence's audio, so that sentences can follow each other on one stream.

A synthesis engine ends a sentence wherever its last sample happens to lie, and starts the next one
the same way. When the output device keeps one stream open between sentences, the step from that
sample to the silence around it - and from the silence to the next sentence's first sample - is
a click if either is not close to zero. `fade_edges` ramps the first and the last few milliseconds
of each sentence linearly through zero. It is a few thousand samples of arithmetic per sentence,
costs the stream no more than the length of the fade in delay (the tail is held back so it can be
ramped), and with `fade_ms=0` it passes the audio through untouched.

PCM16 little-endian mono, the engines' format. A chunk may end in the middle of a sample (a stray
odd byte): that byte is carried to the next chunk, not dropped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np

#: Longer fades start to blur the first and last phonemes of a sentence; the click this is
#: meant to prevent is a step of a single sample.
MAX_FADE_MS = 20.0


def fade_samples(sample_rate: int, fade_ms: float) -> int:
    """How many samples a fade of `fade_ms` covers at `sample_rate`."""
    return max(0, int(round(sample_rate * min(fade_ms, MAX_FADE_MS) / 1000.0)))


def _ramp_in(samples: np.ndarray, first_index: int, length: int) -> np.ndarray:
    """`samples` (int16) whose absolute positions start at `first_index`, faded in over
    `length` samples: position 0 is silent, position `length` is at full level."""
    if first_index >= length or samples.size == 0:
        return samples
    count = min(samples.size, length - first_index)
    gain = (first_index + np.arange(count, dtype=np.float64)) / length
    out = samples.copy()
    out[:count] = np.rint(samples[:count].astype(np.float64) * gain).astype(np.int16)
    return out


def _ramp_out(samples: np.ndarray, length: int) -> np.ndarray:
    """`samples` (the last of a stream) faded out so that the final one is silent."""
    if samples.size == 0:
        return samples
    count = min(samples.size, length)
    gain = np.arange(count - 1, -1, -1, dtype=np.float64) / length
    out = samples.copy()
    out[-count:] = np.rint(samples[-count:].astype(np.float64) * gain).astype(np.int16)
    return out


async def fade_edges(
    chunks: AsyncIterator[bytes], sample_rate: int, fade_ms: float
) -> AsyncIterator[bytes]:
    """Pass `chunks` on with the first and last `fade_ms` of the stream faded through silence."""
    length = fade_samples(sample_rate, fade_ms)
    if length == 0:
        async for chunk in chunks:
            yield chunk
        return
    seen = 0  # samples that have come in so far
    held = np.zeros(0, dtype=np.int16)  # the most recent samples, kept back for the fade-out
    odd = b""
    async for chunk in chunks:
        data = odd + chunk
        usable = len(data) - len(data) % 2
        odd = data[usable:]
        if usable == 0:
            continue
        samples = _ramp_in(np.frombuffer(data[:usable], dtype="<i2"), seen, length)
        seen += samples.size
        held = np.concatenate([held, samples]) if held.size else samples
        if held.size > length:
            release, held = held[: held.size - length], held[held.size - length :]
            yield release.astype("<i2", copy=False).tobytes()
    tail = _ramp_out(held, length).astype("<i2", copy=False).tobytes() + odd
    if tail:
        yield tail
