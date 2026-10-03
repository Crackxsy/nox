"""How `FasterWhisperStt` picks the language and what it tells Whisper to expect.

From the product owner's first real session: "Hallo Nox" was transcribed as "Hello Nox", the
answer came back in English, and "Lass uns streamen" arrived as "Lass uns striam". A fake model
stands in for Whisper; what is pinned here is the decision logic, not Whisper's ears.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np

from nox.core.config.assistant import SttConfig
from nox.voice.stt.faster_whisper_engine import FasterWhisperStt, initial_prompt_for


class _FakeWhisper:
    """Detects `detected` with `probability` when no language is given; records every call."""

    def __init__(self, detected: str, probability: float) -> None:
        self.detected = detected
        self.probability = probability
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, _audio: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(kwargs)
        language = kwargs.get("language") or self.detected
        segment = SimpleNamespace(text=f"[{language}]", avg_logprob=-0.1)
        info = SimpleNamespace(
            language=language,
            language_probability=self.probability if kwargs.get("language") is None else 1.0,
            all_language_probs=[(self.detected, self.probability)],
        )
        return [segment], info


def _engine(model: _FakeWhisper, **kwargs: Any) -> FasterWhisperStt:
    engine = FasterWhisperStt(**kwargs)
    engine._model = model  # noqa: SLF001 - the fake stands in for the loaded Whisper model
    return engine


AUDIO = np.zeros(16000, dtype=np.float32)


def test_auto_keeps_german_when_english_is_only_a_guess() -> None:
    model = _FakeWhisper("en", 0.6)
    text, language, _ = _engine(model)._transcribe_sync(AUDIO, None)  # noqa: SLF001
    assert (language, text) == ("de", "[de]")
    assert model.calls[-1]["language"] == "de"


def test_auto_switches_to_english_when_it_clearly_is() -> None:
    model = _FakeWhisper("en", 0.95)
    _, language, _ = _engine(model)._transcribe_sync(AUDIO, None)  # noqa: SLF001
    assert language == "en"
    assert len(model.calls) == 1


def test_a_fixed_language_skips_detection() -> None:
    model = _FakeWhisper("en", 0.99)
    _, language, _ = _engine(model)._transcribe_sync(AUDIO, "de")  # noqa: SLF001
    assert language == "de" and len(model.calls) == 1


def test_the_vocabulary_reaches_whisper_as_its_prompt() -> None:
    model = _FakeWhisper("de", 0.99)
    engine = _engine(model, vocabulary=("Nox", "Nox", "Stream", "Minecraft"))
    engine._transcribe_sync(AUDIO, "de")  # noqa: SLF001
    assert model.calls[-1]["initial_prompt"] == "Gespräch mit Nox über Stream, Minecraft."


def test_no_vocabulary_means_no_prompt() -> None:
    assert initial_prompt_for(()) is None
    assert initial_prompt_for(("Nox",)) is None


def test_german_is_the_default_and_unknown_values_fall_back() -> None:
    assert SttConfig().language == "de"
    assert "streamen" in SttConfig().vocabulary
    assert SttConfig.model_validate({"language": "fr", "model": "huge"}).language == "de"
    assert SttConfig.model_validate({"model": "huge"}).model == "small"
