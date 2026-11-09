"""`nox core` makes itself non-dumpable on Nox-OS as its very first step (`nox.noxos.protect`):
before the event loop, the extensions or anything they start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import nox.entrypoints
from nox import cli
from nox.noxos import protect


def test_nox_core_protects_itself_before_anything_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def protect_first() -> bool:
        order.append("protect")
        return True

    async def run_core(**_kwargs: Any) -> int:
        # Everything the core starts - extensions, workers, subprocesses - starts in here.
        order.append("run_core")
        return 0

    monkeypatch.setattr(protect, "protect_on_nox_os", protect_first)
    monkeypatch.setattr(nox.entrypoints, "run_core", run_core)
    with pytest.raises(SystemExit) as stopped:
        cli.core(profile=None, no_voice=True, user_config=None)
    assert stopped.value.code == 0
    assert order == ["protect", "run_core"]


def test_only_on_nox_os(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(protect, "protect_core", lambda: calls.append(True) or True)
    monkeypatch.setitem(protect._STATE, "protected", None)  # noqa: SLF001
    assert protect.protect_on_nox_os(tmp_path / "nox-os-release") is None
    assert calls == [] and protect.protected() is None
    release = tmp_path / "nox-os-release"
    release.write_text("NOX_OS_VERSION=v0.7.0\n", "utf-8")
    assert protect.protect_on_nox_os(release) is True
    assert calls == [True] and protect.protected() is True


def test_the_protection_module_starts_nothing_on_import() -> None:
    # Imported first thing in `nox core`: standard library only.
    source = Path(protect.__file__).read_text("utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    expected = ["from __future__ import annotations", "import sys", "from pathlib import Path"]
    assert imports == expected
