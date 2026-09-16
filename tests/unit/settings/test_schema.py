"""The editable-settings schema is derived from `NoxConfig`, not hand-maintained."""

from __future__ import annotations

import pytest

from nox.core.config import NoxConfig
from nox.settings.install import build_appliers
from nox.settings.schema import (
    EDITABLE_PATHS,
    LIVE_APPLY_PATHS,
    UnknownSettingError,
    describe,
    describe_all,
    nest,
    read_value,
)


def test_every_editable_path_exists_on_the_config_model() -> None:
    # A typo or a renamed field in `nox.core.config` fails here, not silently in the dashboard.
    for spec in describe_all():
        assert spec.path in EDITABLE_PATHS
        assert spec.group == EDITABLE_PATHS[spec.path]


def test_types_and_options_come_from_the_pydantic_models() -> None:
    assert describe("identity.ui_language").type == "enum"
    assert describe("identity.ui_language").options == ["de", "en"]
    assert describe("identity.name").type == "string"
    assert describe("pet.greeting_enabled").type == "bool"
    assert describe("ai.router.fallback_chain").type == "list[str]"
    assert describe("voice.channels.routing").options == ["private", "stream", "both", "mute"]
    assert "companion" in (describe("security.profile").options or [])


def test_numeric_bounds_come_from_the_field_constraints() -> None:
    rate = describe("voice.tts.rate")
    assert rate.type == "float"
    assert (rate.min, rate.max) == (0.0, 4.0)  # gt=0.0, le=4.0 on TtsConfig.rate
    volume = describe("voice.tts.volume")
    assert (volume.min, volume.max) == (0.0, 1.0)


def test_restart_required_mirrors_the_live_apply_list() -> None:
    assert describe("privacy.capture.microphone").restart_required is False
    assert describe("pet.variant").restart_required is False
    assert describe("plugins.enabled").restart_required is True
    assert describe("security.profile").restart_required is True


def test_unknown_or_non_editable_paths_are_refused() -> None:
    with pytest.raises(UnknownSettingError):
        describe("security.hard_prohibitions")  # exists, but is deliberately not editable
    with pytest.raises(UnknownSettingError):
        describe("identity.nope")


def test_read_value_reads_the_live_config(config: NoxConfig) -> None:
    assert read_value(config, "identity.ui_language") == config.identity.ui_language
    assert read_value(config, "voice.tts.rate") == config.voice.tts.rate


def test_nest_builds_a_config_layer_fragment() -> None:
    assert nest("privacy.capture.camera", True) == {"privacy": {"capture": {"camera": True}}}


def test_install_provides_an_applier_for_every_live_apply_path() -> None:
    """A path promised as "no restart needed" without an applier would be a fake capability."""

    class _Core:
        config = NoxConfig()
        orchestrator = None
        security = None

    assert set(build_appliers(_Core())) == set(LIVE_APPLY_PATHS)
