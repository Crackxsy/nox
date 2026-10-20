"""Piper stands in when the configured TTS engine cannot load - and says so in its health."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nox.core.config import VoiceConfig
from nox.core.events import HealthStatus
from nox.voice.base import TtsRequest
from nox.voice.tts.fallback import FallbackTts
from nox.worker import main as worker_main
from nox.worker.main import build_piper, load_tts


class FakeEngine:
    def __init__(self, engine_id: str, *, fails: bool = False, rate: int = 22050) -> None:
        self.id = engine_id
        self.fails = fails
        self.sample_rate = rate
        self.loaded = False
        self.loaded_voices = ["v"] if not fails else []
        self.load_time_ms = 12.0

    async def health(self) -> tuple[HealthStatus, str]:
        if not self.loaded:
            return HealthStatus.UNAVAILABLE, "not loaded"
        return HealthStatus.AVAILABLE, "voices: v"

    async def load(self) -> None:
        if self.fails:
            raise FileNotFoundError(f"{self.id} model files missing")
        self.loaded = True

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        yield f"{self.id}:{request.text}".encode()

    async def unload(self) -> None:
        self.loaded = False


@pytest.fixture
def piper(monkeypatch: pytest.MonkeyPatch) -> list[FakeEngine]:
    built: list[FakeEngine] = []

    def fake_build_piper(voice: VoiceConfig, *, configured_voice: bool = True) -> FakeEngine:
        assert configured_voice is False  # the failed engine's voice name means nothing to Piper
        engine = FakeEngine("piper", fails=getattr(fake_build_piper, "fails", False))
        built.append(engine)
        return engine

    monkeypatch.setattr(worker_main, "build_piper", fake_build_piper)
    return built


async def test_a_loadable_engine_is_used_as_it_is(piper: list[FakeEngine]) -> None:
    engine = FakeEngine("supertonic")
    assert await load_tts(engine, VoiceConfig()) is engine
    assert piper == []


async def test_a_failing_engine_falls_back_to_piper_and_reports_limited(
    piper: list[FakeEngine],
) -> None:
    tts = await load_tts(FakeEngine("supertonic", fails=True), VoiceConfig())
    assert isinstance(tts, FallbackTts)
    assert tts.id == "piper" and tts.sample_rate == 22050 and tts.loaded_voices == ["v"]
    status, reason = await tts.health()
    assert status is HealthStatus.LIMITED
    assert "supertonic unavailable" in reason and "model files missing" in reason
    assert "piper" in reason
    chunks = [c async for c in tts.synthesize(TtsRequest(utterance_id="u", text="Hallo."))]
    assert chunks == [b"piper:Hallo."]


async def test_piper_itself_failing_is_not_hidden(piper: list[FakeEngine]) -> None:
    with pytest.raises(FileNotFoundError, match="piper"):
        await load_tts(FakeEngine("piper", fails=True), VoiceConfig())
    assert piper == []


async def test_when_the_fallback_fails_too_the_original_error_is_raised(
    piper: list[FakeEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_main.build_piper, "fails", True, raising=False)
    with pytest.raises(FileNotFoundError, match="supertonic"):
        await load_tts(FakeEngine("supertonic", fails=True), VoiceConfig())


async def test_an_unavailable_fallback_reports_its_own_health() -> None:
    inner = FakeEngine("piper")
    tts = FallbackTts(inner, wanted="supertonic", reason="missing")
    assert (await tts.health())[0] is HealthStatus.UNAVAILABLE
    await tts.load()
    assert (await tts.health())[0] is HealthStatus.LIMITED
    await tts.unload()
    assert inner.loaded is False


def test_the_fallback_piper_ignores_the_other_engines_voice(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate(
        {"models_dir": str(tmp_path), "tts": {"engine": "supertonic", "voice": "F3"}}
    )
    fallback = build_piper(cfg, configured_voice=False)
    assert "F3" not in fallback.voice_names.values()
    assert fallback.models_dir == tmp_path / "piper"
    assert build_piper(cfg).voice_names == {"de": "F3"}
