"""Hardware tests: real devices and models. Skipped unless `-m hardware` is requested explicitly."""

from __future__ import annotations

import time

import numpy as np
import pytest

from nox.voice.base import Channel, TtsRequest

pytestmark = pytest.mark.hardware


def _hardware_requested(request: pytest.FixtureRequest) -> bool:
    expr = request.config.getoption("-m", default="") or ""
    return "hardware" in str(expr) and "not hardware" not in str(expr)


@pytest.fixture(autouse=True)
def _skip_unless_requested(request: pytest.FixtureRequest) -> None:
    if not _hardware_requested(request):
        pytest.skip("hardware tests run only with `-m hardware`")


async def test_devices_and_output_stop_latency() -> None:
    from nox.voice.audio import SoundDeviceOutput, list_devices

    assert any(d["outputs"] != "0" for d in list_devices())
    out = SoundDeviceOutput(volume=0.2)

    async def chunks():  # type: ignore[no-untyped-def]
        t = np.arange(22050 * 3) / 22050
        pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()
        for i in range(0, len(pcm), 4410):
            yield pcm[i : i + 4410]

    import asyncio

    task = asyncio.create_task(out.play(chunks(), 22050, Channel.PRIVATE, utterance_id="hw"))
    await asyncio.sleep(0.5)
    t0 = time.perf_counter()
    await out.stop(reason="test")
    await task
    assert (time.perf_counter() - t0) * 1000 < 200


async def test_piper_and_whisper_roundtrip() -> None:
    from nox.voice.stt.faster_whisper_engine import FasterWhisperStt
    from nox.voice.tts.piper_engine import PiperTts

    tts = PiperTts()
    await tts.load()
    pcm = b"".join(
        [c async for c in tts.synthesize(TtsRequest(utterance_id="x", text="Hallo, ich bin Nox."))]
    )
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    stt = FasterWhisperStt(model_size="base")
    await stt.load()
    t = await stt.transcribe(audio, tts.sample_rate, language="de")
    assert "nox" in t.text.lower() or "knox" in t.text.lower()
