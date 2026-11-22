"""The input side assembled from the configuration, and what the worker reports about it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nox.core.config import VoiceConfig
from nox.core.events import HealthStatus
from nox.voice import vad_silero
from nox.voice.capture_chain import build_capture_chain
from nox.voice.echo import EchoGuardedVad, EchoMode, OutputKind
from nox.voice.vad_silero import SileroModelError, SileroVad
from tests.unit.voice.test_echo import FakeProbe
from tests.unit.voice.test_vad_silero import ScriptedSession, block


def voice_config(tmp_path: Path, **vad: Any) -> VoiceConfig:
    return VoiceConfig.model_validate({"models_dir": str(tmp_path), "vad": vad})


def build(voice: VoiceConfig) -> Any:
    """Headphones on, looked at at once: only the detector is in play."""
    return build_capture_chain(
        voice,
        sample_rate=16000,
        end_silence_ms=450,
        max_utterance_ms=9000,
        min_utterance_ms=300,
        probe=FakeProbe(OutputKind.HEADPHONES),
        spawn=lambda work: work(),
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
    assert isinstance(seg.vad, EchoGuardedVad) and seg.vad.inner is chain.vad


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


# ---- the echo rule in the chain -----------------------------------------------------------------


def chain_with_probe(
    tmp_path: Path, kind: OutputKind, *, run: bool = True, **voice: Any
) -> tuple[Any, FakeProbe, list[Callable[[], None]]]:
    queued: list[Callable[[], None]] = []
    probe = FakeProbe(kind)
    raw = {"models_dir": str(tmp_path), "vad": {"engine": "energy"}, **voice}
    chain = build_capture_chain(
        VoiceConfig.model_validate(raw),
        sample_rate=16000,
        end_silence_ms=450,
        max_utterance_ms=9000,
        min_utterance_ms=300,
        probe=probe,
        spawn=queued.append,
    )
    if run:
        for work in list(queued):
            work()
    return chain, probe, queued


def test_the_chain_looks_at_the_output_device_in_the_background(tmp_path: Path) -> None:
    chain, probe, queued = chain_with_probe(tmp_path, OutputKind.LOUDSPEAKER, run=False)
    assert len(queued) == 1 and probe.asked == []  # nothing blocked the caller
    queued[0]()
    assert probe.asked == [""] and chain.guard.kind is OutputKind.LOUDSPEAKER


def test_the_configured_private_device_is_the_one_that_is_looked_at(tmp_path: Path) -> None:
    _, probe, _ = chain_with_probe(
        tmp_path, OutputKind.HEADPHONES, channels={"private_device": "desk"}
    )
    assert probe.asked == ["desk"]


def test_loudspeakers_add_the_headphones_hint_to_the_notes(tmp_path: Path) -> None:
    chain, _, _ = chain_with_probe(tmp_path, OutputKind.LOUDSPEAKER)
    assert chain.notes().startswith("headphones recommended")


def test_notes_about_the_detector_and_the_output_are_joined(tmp_path: Path) -> None:
    chain, _, _ = chain_with_probe(tmp_path, OutputKind.LOUDSPEAKER, vad={"engine": "silero"})
    notes = chain.notes()
    assert notes.startswith("speech detection: energy fallback")
    assert "; headphones recommended" in notes


def test_headphones_leave_the_notes_empty(tmp_path: Path) -> None:
    chain, _, _ = chain_with_probe(tmp_path, OutputKind.HEADPHONES)
    assert chain.notes() == ""


@pytest.mark.parametrize(
    ("routing", "listens"),
    [("private", True), ("both", True), ("stream", False), ("mute", False)],
)
def test_only_routing_that_plays_here_is_looked_at(
    tmp_path: Path, routing: str, listens: bool
) -> None:
    chain, probe, queued = chain_with_probe(
        tmp_path, OutputKind.LOUDSPEAKER, channels={"routing": routing}
    )
    assert (probe.asked == [""]) is listens
    assert (chain.guard.kind is OutputKind.LOUDSPEAKER) is listens
    if not listens:
        assert chain.guard.kind is OutputKind.SILENT
        assert chain.notes() == ""


def test_the_configured_rule_reaches_the_guard(tmp_path: Path) -> None:
    chain, _, _ = chain_with_probe(
        tmp_path,
        OutputKind.HEADPHONES,
        echo={"mode": "half_duplex", "start_db": -25, "min_ms": 300, "tail_ms": 500},
    )
    params = chain.guard.params
    assert (params.mode, params.start_db, params.min_ms, params.tail_ms) == (
        EchoMode.HALF_DUPLEX,
        -25.0,
        300,
        500,
    )


def test_the_segmenter_is_wrapped_in_the_rule_and_follows_the_pipeline_state(
    tmp_path: Path,
) -> None:
    chain, _, _ = chain_with_probe(tmp_path, OutputKind.LOUDSPEAKER)
    state = {"speaking": False}
    chain.bind_speaking(lambda: state["speaking"])
    wrapper = chain.segmenter.vad
    assert isinstance(wrapper, EchoGuardedVad) and wrapper.inner is chain.vad
    tone = 0.014 * np.sin(2 * np.pi * 220 * np.arange(480) / 16000)
    echo_frame = tone.astype(np.float32)  # about -40 dBFS
    assert [wrapper.is_speech(echo_frame) for _ in range(12)] == [True] * 12
    state["speaking"] = True
    assert not any(wrapper.is_speech(echo_frame) for _ in range(12))
