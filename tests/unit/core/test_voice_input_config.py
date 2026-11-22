"""The configuration of the voice input side: speech detector."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nox.core.config import NoxConfig, VoiceConfig

DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"


def shipped() -> VoiceConfig:
    raw = yaml.safe_load(DEFAULTS.read_text("utf-8"))
    return NoxConfig.model_validate(raw).voice


def test_the_shipped_detector_is_silero_with_hysteresis_and_a_budget() -> None:
    vad = shipped().vad
    assert (vad.engine, vad.model) == ("silero", "")
    assert (vad.threshold, vad.threshold_off, vad.budget_ms) == (0.5, 0.35, 40.0)


def test_the_code_defaults_match_the_shipped_file() -> None:
    assert VoiceConfig().vad == shipped().vad


def test_the_off_threshold_may_not_be_above_the_on_threshold() -> None:
    with pytest.raises(ValidationError, match="threshold must not be smaller than threshold_off"):
        VoiceConfig.model_validate({"vad": {"threshold": 0.3, "threshold_off": 0.6}})
    VoiceConfig.model_validate({"vad": {"threshold": 0.4, "threshold_off": 0.4}})


@pytest.mark.parametrize(
    "bad",
    [
        {"engine": "webrtc"},
        {"threshold": 0.0},
        {"threshold": 1.0},
        {"threshold_off": 0.0},
        {"budget_ms": 0},
        {"budget_ms": 501},
        {"unknown_key": 1},
    ],
)
def test_nonsense_in_the_detector_section_is_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VoiceConfig.model_validate({"vad": bad})


# ---- the echo rule ------------------------------------------------------------------------------


def test_the_shipped_echo_rule_is_automatic_with_a_threshold_and_a_tail() -> None:
    echo = shipped().echo
    assert (echo.mode, echo.start_db, echo.min_ms, echo.tail_ms) == ("auto", -30.0, 240, 400)


def test_the_code_defaults_for_the_echo_rule_match_the_shipped_file() -> None:
    assert VoiceConfig().echo == shipped().echo


@pytest.mark.parametrize("mode", ["auto", "off", "threshold", "half_duplex"])
def test_every_echo_mode_is_accepted(mode: str) -> None:
    assert VoiceConfig.model_validate({"echo": {"mode": mode}}).echo.mode == mode


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "aec"},
        {"start_db": 1},
        {"start_db": -91},
        {"min_ms": -1},
        {"min_ms": 2001},
        {"tail_ms": -1},
        {"tail_ms": 3001},
        {"unknown_key": 1},
    ],
)
def test_nonsense_in_the_echo_section_is_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VoiceConfig.model_validate({"echo": bad})


def test_the_sentence_ends_after_400_ms_of_silence_and_the_pipeline_gets_it() -> None:
    from nox.worker.main import pipeline_config_from

    assert shipped().vad.end_silence_ms == 400
    assert pipeline_config_from(VoiceConfig()).end_silence_ms == 400
    custom = VoiceConfig.model_validate({"vad": {"end_silence_ms": 650}})
    assert pipeline_config_from(custom).end_silence_ms == 650


@pytest.mark.parametrize("bad", [99, 3001])
def test_the_end_of_speech_silence_is_bounded(bad: int) -> None:
    with pytest.raises(ValidationError):
        VoiceConfig.model_validate({"vad": {"end_silence_ms": bad}})
