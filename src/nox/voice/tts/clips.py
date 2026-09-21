"""Pre-rendered WAV clips shared by every TTS engine.

`TtsRequest.prepared_clip` bypasses synthesis and plays `<clips_dir>/<id>.wav`. Clip ids come from
the core and are validated against a strict pattern before they touch the filesystem - no path
separators, no drive letters, no traversal.
"""

from __future__ import annotations

import asyncio
import re
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from nox.voice.audio import resample_linear

CLIP_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


class ClipNotFoundError(FileNotFoundError):
    pass


def read_clip_pcm16(path: Path, target_rate: int) -> bytes:
    """Read a WAV clip and return mono PCM16 at `target_rate`."""
    with wave.open(str(path), "rb") as wf:
        channels, width, rate, frames = (
            wf.getnchannels(),
            wf.getsampwidth(),
            wf.getframerate(),
            wf.getnframes(),
        )
        raw = wf.readframes(frames)
    if width != 2:
        raise ValueError(
            f"clip {path.name}: only 16-bit PCM WAV is supported (got {width * 8}-bit)"
        )
    pcm = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != target_rate:
        f = resample_linear(pcm.astype(np.float32) / 32768.0, rate, target_rate)
        pcm = (f * 32767.0).clip(-32768, 32767).astype(np.int16)
    return pcm.tobytes()


def chunk_bytes(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] if data else []


async def play_clip(clip_id: str, clips_dir: Path, sample_rate: int) -> AsyncIterator[bytes]:
    """Yield ~100 ms PCM16 chunks of a validated clip, resampled to the engine's rate."""
    if not CLIP_ID.match(clip_id):
        raise ValueError(f"invalid clip id {clip_id!r}")
    path = clips_dir / f"{clip_id}.wav"
    if not path.is_file():
        raise ClipNotFoundError(str(path))
    pcm = await asyncio.to_thread(read_clip_pcm16, path, sample_rate)
    for chunk in chunk_bytes(pcm, sample_rate // 10 * 2):
        yield chunk
