"""`nox.paths.app_dir`: one per-user application folder per operating system."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.config import PathsConfig, expand_path
from nox.paths import app_dir, platform_app_dir, resolve_config_paths, user_config_path

HOME = Path("/home/someone")


def test_windows_uses_appdata_like_every_earlier_release() -> None:
    environ = {"APPDATA": r"C:\Users\someone\AppData\Roaming"}
    assert platform_app_dir("win32", environ, HOME) == Path(environ["APPDATA"]) / "Nox"


def test_windows_without_appdata_falls_back_below_the_home_directory() -> None:
    assert platform_app_dir("win32", {}, HOME) == HOME / "AppData" / "Roaming" / "Nox"


def test_macos_uses_application_support() -> None:
    expected = HOME / "Library" / "Application Support" / "Nox"
    assert platform_app_dir("darwin", {"APPDATA": "ignored"}, HOME) == expected


def test_linux_follows_xdg_data_home() -> None:
    environ = {"XDG_DATA_HOME": "/data/xdg"}
    assert platform_app_dir("linux", environ, HOME) == Path("/data/xdg/nox")


def test_linux_ignores_a_relative_xdg_data_home_as_the_specification_requires() -> None:
    environ = {"XDG_DATA_HOME": "relative/dir"}
    assert platform_app_dir("linux", environ, HOME) == HOME / ".local/share" / "nox"


def test_linux_without_xdg_uses_local_share() -> None:
    assert platform_app_dir("linux", {}, HOME) == HOME / ".local/share" / "nox"


def test_nox_app_dir_overrides_the_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_APP_DIR", str(tmp_path / "portable"))
    assert app_dir() == tmp_path / "portable"
    assert user_config_path() == tmp_path / "portable" / "user.yaml"


def test_the_shipped_defaults_resolve_below_the_app_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_APP_DIR", str(tmp_path))
    paths = PathsConfig.model_validate({})
    assert paths.data_dir == tmp_path
    assert paths.runtime_dir == tmp_path / "runtime"
    assert paths.vault_dir == tmp_path / "vault"


def test_nox_app_dir_reference_resolves_without_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`${NOX_APP_DIR}` in a config file never stays literal, even when nothing sets it."""
    monkeypatch.delenv("NOX_APP_DIR", raising=False)
    resolved = expand_path("${NOX_APP_DIR}/runtime")
    assert "$" not in str(resolved)
    assert resolved == app_dir() / "runtime"


def test_config_resolution_finds_the_user_layer_in_the_app_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOX_APP_DIR", str(tmp_path))
    (tmp_path / "user.yaml").write_text("identity:\n  name: Kumo\n", encoding="utf-8")
    _, user = resolve_config_paths()
    assert user == tmp_path / "user.yaml"
