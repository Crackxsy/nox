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


#: What an engine puts around the speech counts as silence below this level (dBFS, on int16
#: samples): digital silence and the faint noise floor of a synthesis model, far below speech.
SILENCE_THRESHOLD_DB = -50.0
#: How much of the silence in front of the first sound and behind the last one is kept. A little in
#: front keeps the attack of the first phoneme intact; a little behind lets the last one decay.
LEAD_KEEP_MS = 10.0
TAIL_KEEP_MS = 60.0


async def trim_silence(
    chunks: AsyncIterator[bytes],
    sample_rate: int,
    *,
    threshold_db: float = SILENCE_THRESHOLD_DB,
    lead_ms: float = LEAD_KEEP_MS,
    tail_ms: float = TAIL_KEEP_MS,
) -> AsyncIterator[bytes]:
    """Pass `chunks` on without the silence at the start and at the end of the sentence.

    Speech engines leave half a second or more of silence in front of a sentence and behind it
    (measured on the default engine: about 0.5 s in front, 0.6 to 0.7 s behind). Played one after
    the other, that is more than a second of dead air between two sentences, however well the next
    one was prepared. Silence is anything under `threshold_db`; `lead_ms` of it in front and
    `tail_ms` behind are kept, and a pause in the middle of the sentence is never touched. A
    sentence that is silent throughout gives nothing. The tail is held back until the stream ends,
    so it costs the end of a sentence nothing but the wait for the engine to finish.
    """
    threshold = 32768.0 * 10.0 ** (threshold_db / 20.0)
    lead = int(round(sample_rate * lead_ms / 1000.0))
    tail = int(round(sample_rate * tail_ms / 1000.0))
    started = False
    quiet = np.zeros(0, dtype="<i2")  # silence seen since the last sound (or before the first)
    odd = b""
    async for chunk in chunks:
        data = odd + chunk
        usable = len(data) - len(data) % 2
        odd = data[usable:]
        if usable == 0:
            continue
        samples = np.frombuffer(data[:usable], dtype="<i2")
        loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > threshold)
        if loud.size == 0:
            quiet = np.concatenate([quiet, samples]) if quiet.size else samples
            if not started:
                quiet = quiet[-lead:] if lead else quiet[:0]
            continue
        first, last = int(loud[0]), int(loud[-1])
        if started:
            head = np.concatenate([quiet, samples[:first]])  # a pause inside the sentence
        else:
            started = True
            before = np.concatenate([quiet, samples[:first]])
            head = before[-lead:] if lead else before[:0]
        out = np.concatenate([head, samples[first : last + 1]])
        quiet = samples[last + 1 :]
        yield out.astype("<i2", copy=False).tobytes()
    if started and tail and quiet.size:
        yield quiet[:tail].astype("<i2", copy=False).tobytes()
