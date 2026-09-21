"""The `home` configuration section, its editable paths and its place in the security profiles.

Three separate promises are checked: the shipped defaults are the ones the plugin would use, the
four settings the Settings page offers are typed and bounded, and the access token is a credential
the Settings page may write but never read back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.core.config import HomeConfig, NoxConfig, load_config
from nox.security.profiles import load_profile
from nox.settings.schema import EDITABLE_PATHS, LIVE_APPLY_PATHS, describe
from nox.settings.secrets_ipc import KNOWN_SECRETS

DEFAULTS = REPO_ROOT / "config" / "defaults.yaml"
HOME_PATHS = ["home.host", "home.port", "home.tls", "home.areas_allowed"]


def test_the_defaults_layer_carries_the_home_section() -> None:
    config = load_config(DEFAULTS)
    assert config.warnings == []
    assert config.home.host == "127.0.0.1"
    assert config.home.port == 8123
    assert config.home.tls is False
    assert config.home.areas_allowed == []


def test_the_home_section_is_a_field_of_the_config_model() -> None:
    """`defaults.yaml` and `NoxConfig` must agree section for section."""
    raw = yaml.safe_load(Path(DEFAULTS).read_text(encoding="utf-8"))
    assert "home" in raw
    assert "home" in NoxConfig.model_fields


def test_the_connection_defaults_point_at_loopback_not_at_a_guessed_address() -> None:
    """Nox never guesses where someone's Home Assistant is; loopback is the only safe default."""
    assert HomeConfig().host == "127.0.0.1"


def test_backoff_bounds_are_ordered() -> None:
    with pytest.raises(ValueError, match="min_backoff_s"):
        HomeConfig(min_backoff_s=30.0, max_backoff_s=1.0)


@pytest.mark.parametrize("path", HOME_PATHS)
def test_every_home_setting_is_editable_typed_and_never_applied_live(path: str) -> None:
    """The plugin worker reads these at start, so none of them can take effect without a restart."""
    assert EDITABLE_PATHS[path] == "home"
    spec = describe(path)
    assert spec.type in ("string", "int", "bool", "list[str]")
    assert spec.restart_required is True
    assert path not in LIVE_APPLY_PATHS


def test_the_port_is_bounded_so_a_typo_cannot_be_saved() -> None:
    spec = describe("home.port")
    assert (spec.min, spec.max) == (1.0, 65535.0)


def test_the_access_token_is_a_managed_secret_and_not_a_setting() -> None:
    assert KNOWN_SECRETS["nox/home/access_token"] == "home"
    assert not any(path.startswith("home.") and "token" in path for path in EDITABLE_PATHS)


def test_the_companion_profile_allows_both_shipped_home_assistant_addresses() -> None:
    companion = load_profile(PROFILES_DIR / "companion.yaml")
    assert "homeassistant.local:8123" in companion.egress_allowlist
    assert "127.0.0.1:8123" in companion.loopback_allowlist


def test_the_offline_profile_still_reaches_nothing() -> None:
    """Smart-home control is egress; the profile that cuts the network carries no exception."""
    offline = load_profile(PROFILES_DIR / "offline.yaml")
    assert offline.egress_allowlist == []
    assert "127.0.0.1:8123" not in offline.loopback_allowlist
