"""The input side assembled from the configuration, and what the worker reports about it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nox.core.config import VoiceConfig
from nox.core.events import HealthStatus
from nox.voice import vad_silero
from nox.voice.capture_chain import build_capture_chain
from nox.voice.vad_silero import SileroModelError, SileroVad
from tests.unit.voice.test_vad_silero import ScriptedSession, block


def voice_config(tmp_path: Path, **vad: Any) -> VoiceConfig:
    return VoiceConfig.model_validate({"models_dir": str(tmp_path), "vad": vad})


def build(voice: VoiceConfig) -> Any:
    return build_capture_chain(
        voice,
        sample_rate=16000,
        end_silence_ms=450,
        max_utterance_ms=9000,
        min_utterance_ms=300,
    )


@pytest.fixture(autouse=True)
def no_bundled_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever is installed on this machine, the tests decide where the model comes from."""
    monkeypatch.setattr(vad_silero, "bundled_model_path", lambda: None)


def test_the_segmenter_carries_the_pipeline_limits(tmp_path: Path) -> None:
    chain = build(voice_config(tmp_path))
    seg = chain.segmenter
    assert (seg.end_silence_ms, seg.max_segment_ms, seg.min_segment_ms) == (450, 9000, 300)
    assert (seg.sample_rate, seg.frame_ms) == (16000, 30)
    assert seg.vad is chain.vad


def test_without_a_model_the_chain_uses_energy_and_says_so(tmp_path: Path) -> None:
    chain = build(voice_config(tmp_path))
    assert chain.vad.engine == "energy"
    assert chain.notes().startswith("speech detection: energy fallback")
    assert vad_silero.MODEL_FILE in chain.notes()


def test_the_energy_engine_chosen_in_the_settings_is_not_a_note(tmp_path: Path) -> None:
    chain = build(voice_config(tmp_path, engine="energy"))
    assert chain.vad.engine == "energy"
    assert chain.notes() == ""


def test_the_configured_thresholds_reach_the_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "silero").mkdir()
    (tmp_path / "silero" / vad_silero.MODEL_FILE).write_bytes(b"x")
    session = ScriptedSession([0.7, 0.55, 0.45])
    monkeypatch.setattr(vad_silero, "open_session", lambda _path: session)

    chain = build(voice_config(tmp_path, threshold=0.6, threshold_off=0.5, budget_ms=12))

    assert chain.vad.engine == "silero"
    assert chain.notes() == ""
    assert [chain.vad.is_speech(block()) for _ in range(3)] == [True, True, False]
    primary = chain.vad._primary
    assert isinstance(primary, SileroVad)
    assert primary.params.budget_ms == 12


def test_a_model_that_cannot_be_opened_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "silero").mkdir()
    (tmp_path / "silero" / vad_silero.MODEL_FILE).write_bytes(b"x")

    def refuse(_path: Path) -> Any:
        raise SileroModelError("the model cannot be loaded: broken")

    monkeypatch.setattr(vad_silero, "open_session", refuse)
    chain = build(voice_config(tmp_path))
    assert chain.vad.health()[0] is HealthStatus.LIMITED
    assert "the model cannot be loaded: broken" in chain.notes()
    assert not chain.segmenter.vad.is_speech(np.zeros(480, dtype=np.float32))
