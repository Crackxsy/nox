"""Settings with known values are offered as choices, and a value outside them is refused.

"Aktive Plugins" and the backend order were text boxes that expected exact ids, one per line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.config import NoxConfig
from nox.settings.schema import InvalidChoiceError, check_choice, describe


def test_the_plugins_are_offered_from_the_plugins_folder() -> None:
    spec = describe("plugins.enabled")
    assert spec.type == "multi"
    assert spec.options is not None and {"twitch", "obs"} <= set(spec.options)


def test_the_backends_are_a_fixed_choice_and_their_order_matters() -> None:
    chain = describe("ai.router.fallback_chain")
    assert (chain.type, chain.ordered) == ("multi", True)
    assert chain.options == ["claude_code", "ollama", "rules"]
    assert describe("ai.router.default_reasoner").type == "enum"


def test_the_push_to_talk_key_is_recorded_not_typed() -> None:
    assert describe("voice.stt.push_to_talk_hotkey").type == "hotkey"


def test_a_plugin_that_is_not_installed_is_refused() -> None:
    spec = describe("plugins.enabled")
    check_choice(spec, ["twitch", "obs"])
    with pytest.raises(InvalidChoiceError, match="twich"):
        check_choice(spec, ["twich"])


def test_the_voice_is_picked_from_the_installed_piper_voices(tmp_path: Path) -> None:
    piper = tmp_path / "piper"
    piper.mkdir()
    (piper / "de_DE-thorsten-medium.onnx").write_bytes(b"")
    config = NoxConfig.model_validate({"voice": {"models_dir": str(tmp_path)}})
    spec = describe("voice.tts.voice", config)
    assert spec.type == "enum"
    assert spec.options == ["", "de_DE-thorsten-medium"]


def test_kokoro_voices_stay_typed_because_there_is_nothing_to_list(tmp_path: Path) -> None:
    config = NoxConfig.model_validate(
        {"voice": {"models_dir": str(tmp_path), "tts": {"engine": "kokoro"}}}
    )
    assert describe("voice.tts.voice", config).type == "string"
