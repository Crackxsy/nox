"""KokoroTts against a fake `kokoro_onnx` module (#21). No model download, no ONNX Runtime."""

from __future__ import annotations

import asyncio
import sys
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nox.core.events import HealthStatus
from nox.voice.base import TtsRequest
from nox.voice.models import KOKORO_FILES, engine_models_dir
from nox.voice.tts.clips import ClipNotFoundError
from nox.voice.tts.kokoro_engine import DEFAULT_VOICES, KokoroTts

VOICE_BANK = ("af_heart", "am_michael", "bf_emma")


class FakeKokoro:
    """Mimics `kokoro_onnx.Kokoro`: float32 samples plus the sample rate, one call per sentence."""

    instances: list[FakeKokoro] = []

    def __init__(self, model_path: str, voices_path: str) -> None:
        self.model_path = model_path
        self.voices_path = voices_path
        self.calls: list[dict[str, Any]] = []
        self.threads: set[str] = set()
        FakeKokoro.instances.append(self)

    def get_voices(self) -> list[str]:
        return list(VOICE_BANK)

    def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[np.ndarray, int]:
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        self.threads.add(threading.current_thread().name)
        samples = np.linspace(-0.5, 0.5, 240, dtype=np.float32)
        return samples, 24000


@pytest.fixture
def fake_kokoro_module(monkeypatch: pytest.MonkeyPatch) -> type[FakeKokoro]:
    FakeKokoro.instances = []
    module = type(sys)("kokoro_onnx")
    module.Kokoro = FakeKokoro  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kokoro_onnx", module)
    return FakeKokoro


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "models" / "kokoro"
    directory.mkdir(parents=True)
    for name in KOKORO_FILES:
        (directory / name).write_bytes(b"not a real model, only its name matters here")
    return directory


# ---- availability ------------------------------------------------------------------------------


async def test_missing_models_are_unavailable_with_an_actionable_reason(tmp_path: Path) -> None:
    engine = KokoroTts(models_dir=tmp_path / "nowhere")
    status, reason = await engine.health()
    assert status is HealthStatus.UNAVAILABLE
    assert "nowhere" in reason
    for name in KOKORO_FILES:
        assert name in reason
    assert "--download-kokoro" in reason


async def test_load_never_downloads_anything(tmp_path: Path) -> None:
    """A missing model is an error the user resolves, never something Nox fetches on its own."""
    engine = KokoroTts(models_dir=tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError):
        await engine.load()
    assert not (tmp_path / "nowhere").exists()


async def test_missing_package_reports_the_extra(
    models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    engine = KokoroTts(models_dir=models_dir)
    with pytest.raises(ImportError):
        await engine.load()
    status, reason = await engine.health()
    assert status is HealthStatus.UNAVAILABLE
    assert "voice-kokoro" in reason


def test_models_dir_defaults_below_the_configured_models_root(tmp_path: Path) -> None:
    engine = KokoroTts(models_dir=engine_models_dir("kokoro", tmp_path / "data" / "models"))
    assert engine.models_dir == tmp_path / "data" / "models" / "kokoro"


# ---- loading -------------------------------------------------------------------------------------


async def test_load_uses_the_expected_files(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    instance = fake_kokoro_module.instances[-1]
    assert Path(instance.model_path) == models_dir / "kokoro-v1.0.onnx"
    assert Path(instance.voices_path) == models_dir / "voices-v1.0.bin"
    assert engine.sample_rate == 24000
    assert engine.loaded_voices == [DEFAULT_VOICES["de"]]
    await engine.unload()


async def test_german_reports_limited_because_kokoro_has_no_german_voice(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    status, reason = await engine.health()
    assert status is HealthStatus.LIMITED
    assert "de" in reason and "English" in reason
    await engine.unload()


async def test_english_only_configuration_is_available(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir, voices={"en": "am_michael"})
    await engine.load()
    status, reason = await engine.health()
    assert status is HealthStatus.AVAILABLE
    assert "am_michael" in reason
    await engine.unload()


async def test_unknown_voice_fails_loudly(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir, voices={"en": "not_a_voice"})
    with pytest.raises(ValueError, match="not_a_voice"):
        await engine.load()


async def test_unload_releases_the_model(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    await engine.unload()
    assert (await engine.health())[0] is HealthStatus.UNAVAILABLE


# ---- synthesis -----------------------------------------------------------------------------------


async def collect(engine: KokoroTts, request: TtsRequest) -> list[bytes]:
    return [chunk async for chunk in engine.synthesize(request)]


async def test_streams_one_chunk_per_sentence_off_the_event_loop(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    request = TtsRequest(
        utterance_id="u1", text="Erster Satz. Zweiter Satz! Dritter Satz?", language="de"
    )
    chunks = await collect(engine, request)
    instance = fake_kokoro_module.instances[-1]
    assert len(instance.calls) == 3
    assert len(chunks) == 3
    assert all(len(c) == 480 for c in chunks)  # 240 float samples -> 240 * 2 bytes PCM16
    # Synthesis must not run on the event loop thread.
    assert instance.threads and threading.current_thread().name not in instance.threads
    assert all(name.startswith("kokoro-") for name in instance.threads)
    await engine.unload()


async def test_pcm16_conversion_is_correct(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    chunks = await collect(engine, TtsRequest(utterance_id="u1", text="Ein Satz.", language="de"))
    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    expected = np.linspace(-0.5, 0.5, 240, dtype=np.float32) * 32767.0
    assert np.allclose(pcm, expected.astype(np.int16), atol=1)
    await engine.unload()


async def test_voice_and_language_selection(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir, voices={"de": "af_heart", "en": "am_michael"})
    await engine.load()
    await collect(engine, TtsRequest(utterance_id="u1", text="Hallo.", language="de"))
    await collect(engine, TtsRequest(utterance_id="u2", text="Hello.", language="en"))
    await collect(engine, TtsRequest(utterance_id="u3", text="Hi.", language="en", voice="bf_emma"))
    calls = fake_kokoro_module.instances[-1].calls
    assert (calls[0]["voice"], calls[0]["lang"]) == ("af_heart", "en-us")  # no German front end
    assert (calls[1]["voice"], calls[1]["lang"]) == ("am_michael", "en-us")
    assert calls[2]["voice"] == "bf_emma"  # explicit request wins
    await engine.unload()


async def test_rate_is_clamped(models_dir: Path, fake_kokoro_module: type[FakeKokoro]) -> None:
    engine = KokoroTts(models_dir=models_dir, rate=4.0)
    await engine.load()
    await collect(engine, TtsRequest(utterance_id="u1", text="Hallo.", rate=4.0))
    assert fake_kokoro_module.instances[-1].calls[0]["speed"] == 2.0
    await engine.unload()


async def test_empty_text_yields_nothing(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    assert await collect(engine, TtsRequest(utterance_id="u1", text="   ")) == []
    await engine.unload()


async def test_synthesize_before_load_fails(models_dir: Path) -> None:
    engine = KokoroTts(models_dir=models_dir)
    with pytest.raises(RuntimeError, match="load"):
        await collect(engine, TtsRequest(utterance_id="u1", text="Hallo."))


async def test_engine_failure_reaches_the_consumer(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()

    def boom(*_args: Any, **_kwargs: Any) -> tuple[np.ndarray, int]:
        raise RuntimeError("onnx session died")

    monkeypatch.setattr(fake_kokoro_module.instances[-1], "create", boom)
    with pytest.raises(RuntimeError, match="onnx session died"):
        await collect(engine, TtsRequest(utterance_id="u1", text="Hallo."))
    await engine.unload()


# ---- prepared clips ------------------------------------------------------------------------------


def write_clip(path: Path, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.zeros(rate // 2, dtype=np.int16).tobytes())


async def test_prepared_clip_bypasses_synthesis(
    models_dir: Path, tmp_path: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    write_clip(clips / "goal.wav")
    engine = KokoroTts(models_dir=models_dir, clips_dir=clips)
    await engine.load()
    chunks = await collect(
        engine, TtsRequest(utterance_id="u1", text="ignored", prepared_clip="goal")
    )
    assert b"".join(chunks)
    assert fake_kokoro_module.instances[-1].calls == []
    await engine.unload()


@pytest.mark.parametrize("bad", ["../evil", "a/b", "C:\\x", "UPPER", "-leading-dash"])
async def test_prepared_clip_id_validation(models_dir: Path, tmp_path: Path, bad: str) -> None:
    engine = KokoroTts(models_dir=models_dir, clips_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid clip id"):
        await collect(engine, TtsRequest(utterance_id="u1", text="x", prepared_clip=bad))


async def test_missing_clip_raises(models_dir: Path, tmp_path: Path) -> None:
    engine = KokoroTts(models_dir=models_dir, clips_dir=tmp_path)
    with pytest.raises(ClipNotFoundError):
        await collect(engine, TtsRequest(utterance_id="u1", text="x", prepared_clip="missing"))


async def test_synthesis_does_not_block_the_loop(
    models_dir: Path, fake_kokoro_module: type[FakeKokoro]
) -> None:
    """A slow engine must not stall other tasks: the loop keeps ticking while it renders."""
    engine = KokoroTts(models_dir=models_dir)
    await engine.load()
    instance = fake_kokoro_module.instances[-1]
    original = instance.create

    def slow(*args: Any, **kwargs: Any) -> tuple[np.ndarray, int]:
        threading.Event().wait(0.05)
        return original(*args, **kwargs)

    instance.create = slow  # type: ignore[method-assign]
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await collect(engine, TtsRequest(utterance_id="u1", text="Eins. Zwei."))
    task.cancel()
    assert ticks >= 5
    await engine.unload()
