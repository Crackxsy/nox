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
