"""SupertonicTts against a fake `supertonic` module. No model download, no ONNX Runtime."""

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
from nox.voice.models import SUPERTONIC_REQUIRED, SUPERTONIC_VOICES, engine_models_dir
from nox.voice.tts.clips import ClipNotFoundError
from nox.voice.tts.supertonic_engine import (
    BASE_SPEED,
    DEFAULT_STEPS,
    DEFAULT_VOICES,
    MAX_SPEED,
    MIN_SPEED,
    SupertonicTts,
)

SAMPLES = np.linspace(-0.5, 0.5, 441, dtype=np.float32)


class FakeTTS:
    """Mimics `supertonic.TTS`: (1, n) float32 waveform plus durations, one call per sentence."""

    instances: list[FakeTTS] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sample_rate = 44100
        self.calls: list[dict[str, Any]] = []
        self.threads: set[str] = set()
        self.styles: list[str] = []
        FakeTTS.instances.append(self)

    def get_voice_style(self, voice_name: str) -> str:
        self.styles.append(voice_name)
        return f"style:{voice_name}"

    def synthesize(
        self,
        text: str,
        voice_style: str,
        total_steps: int,
        speed: float = BASE_SPEED,
        lang: str = "",
    ) -> tuple[np.ndarray, np.ndarray]:
        self.calls.append(
            {"text": text, "style": voice_style, "steps": total_steps, "speed": speed, "lang": lang}
        )
        self.threads.add(threading.current_thread().name)
        return SAMPLES.reshape(1, -1), np.array([0.01], dtype=np.float32)

    def real_calls(self) -> list[dict[str, Any]]:
        """Calls after the warm-up `load` makes."""
        return self.calls[1:]


@pytest.fixture
def fake_supertonic(monkeypatch: pytest.MonkeyPatch) -> type[FakeTTS]:
    FakeTTS.instances = []
    module = type(sys)("supertonic")
    module.TTS = FakeTTS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "supertonic", module)
    return FakeTTS


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "models" / "supertonic"
    for name in SUPERTONIC_REQUIRED:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"not a real model, only its name matters here")
    (directory / "voice_styles").mkdir()
    for voice in SUPERTONIC_VOICES:
        (directory / "voice_styles" / f"{voice}.json").write_text("{}", encoding="utf-8")
    return directory


async def loaded(models_dir: Path, **kwargs: Any) -> SupertonicTts:
    engine = SupertonicTts(models_dir=models_dir, **kwargs)
    await engine.load()
    return engine


async def collect(engine: SupertonicTts, request: TtsRequest) -> list[bytes]:
    return [chunk async for chunk in engine.synthesize(request)]


# ---- availability --------------------------------------------------------------------------------


async def test_missing_models_are_unavailable_with_an_actionable_reason(tmp_path: Path) -> None:
    engine = SupertonicTts(models_dir=tmp_path / "nowhere")
    status, reason = await engine.health()
    assert status is HealthStatus.UNAVAILABLE
    assert "nowhere" in reason
    assert "onnx/vocoder.onnx" in reason
    assert "voice_styles/F3.json" in reason
    assert "--download-supertonic" in reason
    assert "OpenRAIL-M" in reason


async def test_load_never_downloads_anything(
    tmp_path: Path, fake_supertonic: type[FakeTTS]
) -> None:
    """A missing model is an error the user resolves, never something Nox fetches on its own."""
    engine = SupertonicTts(models_dir=tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError):
        await engine.load()
    assert not (tmp_path / "nowhere").exists()
    assert fake_supertonic.instances == []


async def test_the_package_is_told_never_to_download(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    await loaded(models_dir)
    kwargs = fake_supertonic.instances[-1].kwargs
    assert kwargs["auto_download"] is False
    assert Path(kwargs["model_dir"]) == models_dir
    assert kwargs["model"] == "supertonic-3"


async def test_missing_package_reports_the_extra(
    models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "supertonic", None)
    engine = SupertonicTts(models_dir=models_dir)
    with pytest.raises(ImportError):
        await engine.load()
    status, reason = await engine.health()
    assert status is HealthStatus.UNAVAILABLE
    assert "voice-supertonic" in reason


async def test_unknown_voice_fails_loudly_and_lists_the_installed_ones(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = SupertonicTts(models_dir=models_dir, voices={"de": "Z9"})
    with pytest.raises(ValueError, match=r"Z9.*available: F1, F2"):
        await engine.load()
    assert engine.available_voices() == sorted(SUPERTONIC_VOICES)
    assert fake_supertonic.instances == []


async def test_a_voice_without_its_style_file_is_named(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    (models_dir / "voice_styles" / "M2.json").unlink()
    engine = SupertonicTts(models_dir=models_dir, voices={"de": "M2"})
    status, reason = await engine.health()
    assert status is HealthStatus.UNAVAILABLE
    assert "voice_styles/M2.json" in reason


def test_invalid_tuning_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="steps"):
        SupertonicTts(models_dir=tmp_path, steps=0)
    with pytest.raises(ValueError, match="threads"):
        SupertonicTts(models_dir=tmp_path, threads=0)


def test_models_dir_defaults_below_the_configured_models_root(tmp_path: Path) -> None:
    engine = SupertonicTts(models_dir=engine_models_dir("supertonic", tmp_path / "data" / "models"))
    assert engine.models_dir == tmp_path / "data" / "models" / "supertonic"


# ---- loading -------------------------------------------------------------------------------------


async def test_load_reports_voices_rate_and_tuning(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir, threads=2)
    instance = fake_supertonic.instances[-1]
    assert instance.kwargs["intra_op_num_threads"] == 2
    assert engine.sample_rate == 44100
    assert engine.loaded_voices == [DEFAULT_VOICES["de"]]
    status, reason = await engine.health()
    assert status is HealthStatus.AVAILABLE
    assert "F3" in reason and f"{DEFAULT_STEPS} steps" in reason and "2 CPU threads" in reason
    await engine.unload()
    assert (await engine.health())[0] is HealthStatus.UNAVAILABLE


async def test_load_warms_the_model_up_once(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    """The first real sentence must not pay ONNX Runtime's one-off setup cost."""
    await loaded(models_dir)
    assert len(fake_supertonic.instances[-1].calls) == 1


async def test_one_voice_shared_by_both_languages_is_loaded_once(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    await loaded(models_dir, voices={"de": "M1", "en": "M1"})
    assert fake_supertonic.instances[-1].styles == ["M1"]


# ---- synthesis -----------------------------------------------------------------------------------


async def test_streams_one_chunk_per_sentence_off_the_event_loop(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir)
    request = TtsRequest(
        utterance_id="u1", text="Erster Satz. Zweiter Satz! Dritter Satz?", language="de"
    )
    chunks = await collect(engine, request)
    instance = fake_supertonic.instances[-1]
    assert [c["text"] for c in instance.real_calls()] == [
        "Erster Satz.",
        "Zweiter Satz!",
        "Dritter Satz?",
    ]
    assert len(chunks) == 3
    assert all(len(c) == SAMPLES.size * 2 for c in chunks)
    assert threading.current_thread().name not in instance.threads
    assert any(name.startswith("supertonic-u1") for name in instance.threads)


async def test_pcm16_conversion_is_correct(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir)
    chunks = await collect(engine, TtsRequest(utterance_id="u1", text="Ein Satz.", language="de"))
    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    assert np.allclose(pcm, (SAMPLES * 32767.0).astype(np.int16), atol=1)


async def test_voice_language_steps_and_speed_reach_the_model(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir, voices={"de": "F3", "en": "M1"}, rate=1.2, steps=7)
    await collect(engine, TtsRequest(utterance_id="u1", text="Hallo du.", language="de"))
    await collect(engine, TtsRequest(utterance_id="u2", text="Hello you.", language="en"))
    de, en = fake_supertonic.instances[-1].real_calls()
    assert (de["style"], de["lang"], de["steps"]) == ("style:F3", "de", 7)
    assert (en["style"], en["lang"]) == ("style:M1", "en")
    assert de["speed"] == pytest.approx(BASE_SPEED * 1.2)


async def test_unsupported_language_uses_the_unknown_token_and_the_default_voice(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir)
    await collect(engine, TtsRequest(utterance_id="u1", text="Bonjour toi.", language="xx"))
    call = fake_supertonic.instances[-1].real_calls()[0]
    assert call["lang"] == "na"
    assert call["style"] == "style:F3"


@pytest.mark.parametrize(("rate", "expected"), [(0.1, MIN_SPEED), (10.0, MAX_SPEED)])
async def test_speed_is_clamped_to_what_the_model_accepts(
    models_dir: Path, fake_supertonic: type[FakeTTS], rate: float, expected: float
) -> None:
    engine = await loaded(models_dir)
    await collect(engine, TtsRequest(utterance_id="u1", text="Ein Satz.", rate=rate))
    assert fake_supertonic.instances[-1].real_calls()[0]["speed"] == expected


async def test_an_unloaded_request_voice_falls_back_instead_of_going_silent(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir)
    chunks = await collect(
        engine, TtsRequest(utterance_id="u1", text="Ein Satz.", voice="de_DE-thorsten-medium")
    )
    assert chunks
    assert fake_supertonic.instances[-1].real_calls()[0]["style"] == "style:F3"


async def test_render_is_the_single_sentence_entry_point(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    engine = await loaded(models_dir)
    pcm = await engine.render("  Ein einzelner Satz!  ", language="de")
    assert len(pcm) == SAMPLES.size * 2
    assert fake_supertonic.instances[-1].real_calls()[0]["text"] == "Ein einzelner Satz!"
    assert await engine.render("   ") == b""


async def test_synthesis_before_load_fails_loudly(models_dir: Path) -> None:
    engine = SupertonicTts(models_dir=models_dir)
    with pytest.raises(RuntimeError, match="load"):
        await collect(engine, TtsRequest(utterance_id="u1", text="Ein Satz."))
    with pytest.raises(RuntimeError, match="load"):
        await engine.render("Ein Satz.")


async def test_model_errors_reach_the_consumer(
    models_dir: Path, fake_supertonic: type[FakeTTS], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await loaded(models_dir)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("text too long")

    monkeypatch.setattr(fake_supertonic.instances[-1], "synthesize", broken)
    with pytest.raises(ValueError, match="too long"):
        await collect(engine, TtsRequest(utterance_id="u1", text="Ein Satz."))


async def test_closing_the_stream_stops_after_the_current_sentence(
    models_dir: Path, fake_supertonic: type[FakeTTS]
) -> None:
    """Barge-in: once the consumer stops listening, no further sentence is rendered."""
    engine = await loaded(models_dir)
    instance = fake_supertonic.instances[-1]
    second_started, release, done = threading.Event(), threading.Event(), threading.Event()
    original = instance.synthesize

    def held_from_the_second_sentence(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if len(instance.real_calls()) >= 2:
            second_started.set()
            release.wait(timeout=5)
            done.set()
        return result

    instance.synthesize = held_from_the_second_sentence  # type: ignore[method-assign]
    stream = engine.synthesize(
        TtsRequest(utterance_id="u1", text="Eins ist hier. Zwei ist da. Drei ist dort.")
    )
    assert await stream.__anext__()
    assert await asyncio.to_thread(second_started.wait, 5)
    await stream.aclose()  # the consumer leaves while sentence two is rendering
    release.set()
    assert done.wait(timeout=5)
    await asyncio.sleep(0.05)  # give the worker the moment it needs to see the cancel flag
    assert [c["text"] for c in instance.real_calls()] == ["Eins ist hier.", "Zwei ist da."]


async def test_prepared_clip_bypasses_synthesis(
    models_dir: Path, fake_supertonic: type[FakeTTS], tmp_path: Path
) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    with wave.open(str(clips / "goal.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(np.zeros(4410, dtype=np.int16).tobytes())
    engine = await loaded(models_dir, clips_dir=clips)
    chunks = await collect(engine, TtsRequest(utterance_id="u1", text="", prepared_clip="goal"))
    assert sum(len(c) for c in chunks) == 4410 * 2
    assert fake_supertonic.instances[-1].real_calls() == []
    with pytest.raises(ClipNotFoundError):
        await collect(engine, TtsRequest(utterance_id="u2", text="", prepared_clip="missing"))
