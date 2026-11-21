"""Every place that names the games Nox watches starts from one list.

The two data files (`config/defaults.yaml`, `plugins/rl/manifest.yaml`) cannot import the tuple,
so this is what keeps them from drifting away from it."""

from __future__ import annotations

from pathlib import Path

import yaml

from nox.core.config import NoxConfig
from nox.core.config.assistant import SensorGameConfig
from nox.core.config.features import RlDetectionConfig
from nox.core.config.games import DEFAULT_GAME_PROCESSES
from nox.paths import DEFAULTS_PATH

MANIFEST = Path(__file__).resolve().parents[3] / "plugins" / "rl" / "manifest.yaml"
EXPECTED = list(DEFAULT_GAME_PROCESSES)


def test_the_shared_list_is_not_empty() -> None:
    assert EXPECTED, "an empty default would leave the game lock with nothing to watch"


def test_the_config_models_default_to_the_shared_list() -> None:
    assert SensorGameConfig().process_names == EXPECTED
    assert RlDetectionConfig().process_names == EXPECTED


def test_a_default_list_is_a_copy_nobody_can_change_for_everyone() -> None:
    first, second = SensorGameConfig(), SensorGameConfig()
    first.process_names.append("Other.exe")
    assert second.process_names == EXPECTED
    assert list(DEFAULT_GAME_PROCESSES) == EXPECTED


def test_the_shipped_defaults_file_names_the_shared_list() -> None:
    raw = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    assert raw["sensors"]["game"]["process_names"] == EXPECTED
    assert raw["rl"]["detection"]["process_names"] == EXPECTED


def test_the_loaded_configuration_agrees_with_the_defaults_file() -> None:
    config = NoxConfig.model_validate(yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")))
    assert config.sensors.game.process_names == EXPECTED
    assert config.rl.detection.process_names == EXPECTED


def test_the_plugin_manifest_names_the_shared_list() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["config"]["detection"]["process_names"] == EXPECTED
