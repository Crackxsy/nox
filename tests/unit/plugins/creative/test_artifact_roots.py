"""`creative.artifact.inspect` reads only inside the configured `artifact_roots`.

It used to accept any path on disk, which turned a "metadata of my export" tool into a way to
probe or read any file the user can - a key file, the Nox database - for whoever can call it.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest
from nox_plugin_creative import create

from .conftest import FakeClient, make_api


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 80)
    return path


async def _inspect(roots: list[str], path: Path | str) -> dict[str, Any]:
    api = make_api(FakeClient(), artifact_roots=roots)
    create(api)
    result: dict[str, Any] = await api.tools.call("creative.artifact.inspect", {"path": str(path)})
    return result


async def test_a_file_inside_a_root_is_inspected(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    result = await _inspect([str(exports)], _wav(exports / "mix.wav"))
    assert result["ok"] is True and result["artefact_type"] == "audio"


async def test_a_file_outside_every_root_is_refused_without_being_read(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    secret = _wav(tmp_path / "elsewhere.wav")
    result = await _inspect([str(exports)], secret)
    assert result["ok"] is False and result["refused"] is True
    assert result["metadata"] == {}


async def test_no_roots_means_no_reads(tmp_path: Path) -> None:
    result = await _inspect([], _wav(tmp_path / "mix.wav"))
    assert result["refused"] is True


@pytest.mark.parametrize("relative", ["exports/../elsewhere.wav", "mix.wav"])
async def test_traversal_and_relative_paths_are_refused(tmp_path: Path, relative: str) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    _wav(tmp_path / "elsewhere.wav")
    target = tmp_path / relative if relative.startswith("exports") else relative
    result = await _inspect([str(exports)], target)
    assert result["refused"] is True


async def test_a_symlink_that_leads_out_of_a_root_is_refused(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    outside = _wav(tmp_path / "private.wav")
    link = exports / "innocent.wav"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this platform does not allow creating symlinks here")
    result = await _inspect([str(exports)], link)
    assert result["refused"] is True
