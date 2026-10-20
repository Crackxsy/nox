"""Package tools: fixed argv shapes, strict id validation, and the never-remember guarantee."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nox.hands.packages import PackageInput, apt_argv, register_package_tools, winget_argv
from nox.tools.registry import ToolRegistry


def test_winget_pins_the_source() -> None:
    argv = winget_argv("install", "VideoLAN.VLC")
    assert argv[:2] == ["winget", "install"]
    assert "--source" in argv and argv[argv.index("--source") + 1] == "winget"
    assert "VideoLAN.VLC" in argv


def test_apt_matches_the_sudoers_shape() -> None:
    assert apt_argv("install", "vlc") == ["sudo", "-n", "apt-get", "install", "-y", "vlc"]
    assert apt_argv("remove", "vlc") == ["sudo", "-n", "apt-get", "remove", "-y", "vlc"]


@pytest.mark.parametrize("bad", ["vlc; rm -rf /", "a b", "foo&&bar", "../x", "$(id)", ""])
def test_package_id_rejects_shell_metacharacters(bad: str) -> None:
    with pytest.raises(ValidationError):
        PackageInput.model_validate({"package_id": bad})


def test_valid_ids_accepted() -> None:
    for good in ["VideoLAN.VLC", "git", "python3.11", "lib-foo_bar+x"]:
        assert PackageInput.model_validate({"package_id": good}).package_id == good


def test_registered_tools_are_not_rememberable() -> None:
    registry = ToolRegistry()

    async def fake_run(argv: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    register_package_tools(registry, fake_run)
    for name in ("package.install", "package.remove"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.rememberable is False  # "always allow" for installs must never exist
