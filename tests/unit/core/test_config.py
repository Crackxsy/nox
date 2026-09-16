"""nox.core.config: four-layer merge, fallback on invalid layers, hard-prohibition rule, env
paths."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nox.core.config import ConfigError, NoxConfig, SecurityConfig, deep_merge, load_config
from nox.security.hardlist import HARD_PROHIBITIONS

DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"


def write_yaml(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_defaults_load_and_match_schema() -> None:
    cfg = load_config(DEFAULTS)
    assert cfg.warnings == []
    assert cfg.identity.name == "Nox"
    assert cfg.ipc.port == 47800
    assert cfg.supervisor.control_port == 47799
    assert set(cfg.security.hard_prohibitions) == HARD_PROHIBITIONS
    assert cfg.attention.per_mode["rocket_league"] == 1
    assert cfg.logging.json_output is True
    assert cfg.log_retention_days == 14
    # every top-level section of defaults.yaml is a field of NoxConfig (nothing silently ignored)
    raw = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
    assert set(raw) == set(NoxConfig.model_fields)


def test_egress_allowlists_have_defaults_and_are_overridable(tmp_path: Path) -> None:
    """OP-7 C / B-11: both lists exist in defaults.yaml, and a config without them still validates
    (the model's own default is an empty global list, Ollama on the loopback list)."""
    cfg = load_config(DEFAULTS)
    # The single shipped global entry is the user-initiated Twitch OAuth login (EPIC-21) - a
    # deliberate, documented default (docs/PRIVACY.md), not a host Nox ever contacts on its own.
    assert cfg.security.egress_allowlist == ["id.twitch.tv:443"]
    assert cfg.security.loopback_allowlist == ["127.0.0.1:11434"]

    bare = SecurityConfig()
    assert bare.egress_allowlist == [] and bare.loopback_allowlist == ["127.0.0.1:11434"]

    user = write_yaml(
        tmp_path / "user.yaml",
        {
            "security": {
                "egress_allowlist": ["api.anthropic.com:443"],
                "loopback_allowlist": ["127.0.0.1:11434", "127.0.0.1:4455"],
            }
        },
    )
    cfg = load_config(DEFAULTS, user)
    assert cfg.warnings == []
    assert cfg.security.egress_allowlist == ["api.anthropic.com:443"]
    assert cfg.security.loopback_allowlist == ["127.0.0.1:11434", "127.0.0.1:4455"]


def test_deep_merge_nested_dicts_replace_lists() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2}
    merged = deep_merge(base, {"a": {"y": [9]}, "c": 3})
    assert merged == {"a": {"x": 1, "y": [9]}, "b": 2, "c": 3}
    assert base["a"]["y"] == [1, 2]  # input untouched


def test_user_layer_overrides_and_keeps_siblings(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"ipc": {"port": 50000}, "identity": {"name": "Nix"}})
    cfg = load_config(DEFAULTS, user)
    assert cfg.warnings == []
    assert cfg.ipc.port == 50000
    assert cfg.ipc.http_port == 47801  # sibling from defaults kept
    assert cfg.identity.name == "Nix"
    assert cfg.identity.ui_language == "de"


def test_env_expansion_in_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOX_TEST_ROOT", str(tmp_path))
    user = write_yaml(tmp_path / "user.yaml", {"paths": {"logs_dir": "${NOX_TEST_ROOT}/logs"}})
    cfg = load_config(DEFAULTS, user)
    assert cfg.paths.logs_dir == tmp_path / "logs"


def test_invalid_user_yaml_falls_back_with_warning(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    user.write_text("ipc: [unclosed", encoding="utf-8")
    cfg = load_config(DEFAULTS, user)
    assert cfg.ipc.port == 47800
    assert len(cfg.warnings) == 1
    assert cfg.warnings[0].layer == "user"
    assert "YAML" in cfg.warnings[0].message


def test_unknown_key_rejects_layer_with_path(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"ipc": {"port": 50000, "bogus": 1}})
    cfg = load_config(DEFAULTS, user)
    assert cfg.ipc.port == 47800  # whole layer rejected, not partially applied
    assert "ipc.bogus" in cfg.warnings[0].message


def test_missing_user_file_is_a_warning_not_an_error(tmp_path: Path) -> None:
    cfg = load_config(DEFAULTS, tmp_path / "nope.yaml")
    assert cfg.warnings[0].layer == "user"
    assert "not found" in cfg.warnings[0].message


def test_hard_prohibitions_can_only_be_extended(tmp_path: Path) -> None:
    fewer = sorted(HARD_PROHIBITIONS)[:-1]
    user = write_yaml(tmp_path / "user.yaml", {"security": {"hard_prohibitions": fewer}})
    cfg = load_config(DEFAULTS, user)
    assert set(cfg.security.hard_prohibitions) == HARD_PROHIBITIONS
    assert "security.hard_prohibitions" in cfg.warnings[0].message
    assert sorted(HARD_PROHIBITIONS)[-1] in cfg.warnings[0].message

    more = [*sorted(HARD_PROHIBITIONS), "custom.thing"]
    user = write_yaml(tmp_path / "user.yaml", {"security": {"hard_prohibitions": more}})
    cfg = load_config(DEFAULTS, user)
    assert cfg.warnings == []
    assert "custom.thing" in cfg.security.hard_prohibitions
    assert HARD_PROHIBITIONS <= set(cfg.security.hard_prohibitions)


def test_hard_prohibitions_cannot_be_removed_by_override() -> None:
    with pytest.raises(ConfigError, match="hard_prohibitions"):
        load_config(DEFAULTS, overrides={"security": {"hard_prohibitions": []}})


def test_profile_layer_applies_and_user_profiles_shadow(tmp_path: Path) -> None:
    write_yaml(tmp_path / "cfg" / "defaults.yaml", yaml.safe_load(DEFAULTS.read_text("utf-8")))
    write_yaml(
        tmp_path / "cfg" / "profiles" / "stream.yaml", {"attention": {"proactivity_level": 4}}
    )
    cfg = load_config(tmp_path / "cfg" / "defaults.yaml", None, "stream")
    assert cfg.attention.proactivity_level == 4
    assert cfg.profile_id == "stream"

    user = write_yaml(tmp_path / "u" / "user.yaml", {})
    write_yaml(tmp_path / "u" / "profiles" / "stream.yaml", {"attention": {"proactivity_level": 5}})
    cfg = load_config(tmp_path / "cfg" / "defaults.yaml", user, "stream")
    assert cfg.attention.proactivity_level == 5


def test_missing_or_invalid_profile_is_a_warning(tmp_path: Path) -> None:
    cfg = load_config(DEFAULTS, None, "does-not-exist")
    assert cfg.profile_id is None
    assert cfg.warnings[0].layer == "profile"

    write_yaml(tmp_path / "cfg" / "defaults.yaml", yaml.safe_load(DEFAULTS.read_text("utf-8")))
    write_yaml(tmp_path / "cfg" / "profiles" / "bad.yaml", {"attention": {"proactivity_level": 99}})
    cfg = load_config(tmp_path / "cfg" / "defaults.yaml", None, "bad")
    assert cfg.profile_id is None
    assert cfg.attention.proactivity_level == 3
    assert "attention.proactivity_level" in cfg.warnings[0].message


def test_runtime_overrides_apply_in_memory_only(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"ipc": {"port": 50000}})
    before = user.read_text(encoding="utf-8")
    cfg = load_config(
        DEFAULTS, user, overrides={"ipc": {"port": 50001}, "privacy": {"mode": "private"}}
    )
    assert cfg.ipc.port == 50001
    assert cfg.privacy.mode == "private"
    assert user.read_text(encoding="utf-8") == before


def test_invalid_override_raises_with_path() -> None:
    with pytest.raises(ConfigError, match="ipc.port"):
        load_config(DEFAULTS, overrides={"ipc": {"port": 1}})


def test_ollama_gpu_never_in_rocket_league(tmp_path: Path) -> None:
    user = write_yaml(
        tmp_path / "user.yaml",
        {"ai": {"providers": {"ollama": {"gpu_allowed_modes": ["companion", "rocket_league"]}}}},
    )
    cfg = load_config(DEFAULTS, user)
    assert "rocket_league" not in cfg.ai.providers.ollama.gpu_allowed_modes
    assert "rocket_league" in cfg.warnings[0].message


def test_invalid_defaults_raise(tmp_path: Path) -> None:
    bad = write_yaml(tmp_path / "defaults.yaml", {"ipc": {"port": "nope"}})
    with pytest.raises(ConfigError, match="ipc.port"):
        load_config(bad)
    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(not_mapping)


def test_logging_json_alias_and_level_case(tmp_path: Path) -> None:
    user = write_yaml(tmp_path / "user.yaml", {"logging": {"json": False, "level": "debug"}})
    cfg = load_config(DEFAULTS, user)
    assert cfg.logging.json_output is False
    assert cfg.logging.level == "DEBUG"
