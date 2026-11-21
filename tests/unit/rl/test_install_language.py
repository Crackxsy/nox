"""Which language a Rocket League callout is spoken in follows the speech language setting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nox.rl.install import _speech_language


def _core(speech_language: str) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(identity=SimpleNamespace(speech_language=speech_language))
    )


@pytest.mark.parametrize(
    ("configured", "spoken"), [("de", "de"), ("en", "en"), ("auto", "de"), ("", "de")]
)
def test_callouts_are_spoken_in_a_concrete_language(configured: str, spoken: str) -> None:
    assert _speech_language(_core(configured)) == spoken
