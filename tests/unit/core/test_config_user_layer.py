"""A bad value in user.yaml costs that value only, and never makes Nox less protective."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nox.core.config import NoxConfig, load_config
from nox.core.config.user_layer import FAIL_CLOSED
from nox.security.hardlist import HARD_PROHIBITIONS

DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"


def write_yaml(path: Path, data: object) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_one_invalid_key_keeps_the_rest_of_the_user_layer(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    user = write_yaml(
        tmp_path / "user.yaml",
        {
            "privacy": {"mode": "private"},
            "paths": {"data_dir": str(data_dir)},
            "plugins": {"enabled": ["home"]},
            "ipc": {"port": "not-a-port"},
        },
    )

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.mode == "private"
    assert cfg.paths.data_dir == data_dir
    assert cfg.paths.database_dir == data_dir / "database"
    assert cfg.plugins.enabled == ["home"]
    assert cfg.ipc.port == 47800
    assert [w.key for w in cfg.warnings] == ["ipc.port"]
    assert cfg.warnings[0].message.startswith("ipc.port: ")
    assert "the default applies" in cfg.warnings[0].message


def test_each_invalid_key_gets_its_own_warning(tmp_path: Path) -> None:
    user = write_yaml(
        tmp_path / "user.yaml",
        {"ipc": {"port": "x", "http_port": 1}, "identity": {"ui_language": "fr", "name": "Nix"}},
    )

    cfg = load_config(DEFAULTS, user)

    assert sorted(w.key for w in cfg.warnings) == [
        "identity.ui_language",
        "ipc.http_port",
        "ipc.port",
    ]
    assert cfg.identity.name == "Nix"


def test_an_invalid_privacy_mode_falls_back_to_the_strictest_mode_not_the_default(
    tmp_path: Path,
) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"privacy": {"mode": "ofline"}})

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.mode == "offline"  # the default would be "balanced": weaker
    assert cfg.warnings[0].key == "privacy.mode"
    assert "strictest value used for privacy.mode" in cfg.warnings[0].message


def test_an_invalid_capture_switch_turns_the_device_off(tmp_path: Path) -> None:
    user = write_yaml(
        tmp_path / "user.yaml",
        {"privacy": {"capture": {"microphone": "sometimes", "screen": False}}},
    )

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.capture.microphone is False
    assert cfg.privacy.capture.screen is False


def test_an_invalid_security_profile_uses_the_offline_profile(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"security": {"profile": "godmode"}})

    cfg = load_config(DEFAULTS, user)

    assert cfg.security.profile == "offline"


def test_a_broken_allowlist_entry_opens_nothing(tmp_path: Path) -> None:
    user = write_yaml(
        tmp_path / "user.yaml", {"security": {"egress_allowlist": ["api.example.com:99999"]}}
    )

    cfg = load_config(DEFAULTS, user)

    assert cfg.security.egress_allowlist == []  # not the default list either


def test_rejected_extra_prohibitions_are_kept_rather_than_dropped(tmp_path: Path) -> None:
    fewer_plus_custom = [*sorted(HARD_PROHIBITIONS)[:-1], "custom.thing"]
    user = write_yaml(
        tmp_path / "user.yaml", {"security": {"hard_prohibitions": fewer_plus_custom}}
    )

    cfg = load_config(DEFAULTS, user)

    assert set(cfg.security.hard_prohibitions) == {*HARD_PROHIBITIONS, "custom.thing"}


def test_a_bad_zone_entry_drops_that_entry_only(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"privacy": {"zones": ["banking", {"x": 1}, "vpn"]}})

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.zones == ["banking", "vpn"]
    assert cfg.warnings[0].key == "privacy.zones.1"


def test_a_section_level_error_is_narrowed_to_the_key_that_caused_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOX_TEST_UNSET_VARIABLE", raising=False)
    user = write_yaml(
        tmp_path / "user.yaml",
        {
            "paths": {"data_dir": "${NOX_TEST_UNSET_VARIABLE}/nox"},
            "privacy": {"mode": "private"},
        },
    )

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.mode == "private"
    assert [w.key for w in cfg.warnings] == ["paths.data_dir"]


def test_an_unreadable_user_layer_uses_the_strictest_protective_values(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    user.write_text("privacy: [unclosed", encoding="utf-8")

    cfg = load_config(DEFAULTS, user)

    assert cfg.privacy.mode == "offline"
    assert cfg.privacy.capture.microphone is False
    assert cfg.security.pin_required_for_security_changes is True
    assert cfg.remote.enabled is False
    assert len(cfg.warnings) == 1
    assert "strictest value" in cfg.warnings[0].message


@pytest.mark.parametrize("key", sorted(FAIL_CLOSED))
def test_every_strict_value_is_itself_valid_and_never_weaker_than_the_default(key: str) -> None:
    defaults = NoxConfig()
    below: object = defaults.model_dump(mode="python", by_alias=True)
    for part in key.split("."):
        below = below[part]  # type: ignore[index]
    strict = FAIL_CLOSED[key](None, below)
    section, _, leaf = key.rpartition(".")
    patch: dict[str, object] = {leaf: strict}
    for part in reversed(section.split(".")):
        patch = {part: patch}

    NoxConfig.model_validate(patch)  # raises if the strict value were invalid
    if isinstance(strict, list) and key != "security.egress_allowlist":
        assert set(below) <= set(strict)  # type: ignore[arg-type]
