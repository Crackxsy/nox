"""`TrimBackend` (ST-15-06, Spec v0.6 §11): ffmpeg is not currently installed, so `available()`
must be honest, and `trim()` must fail with `TrimError` rather than fake a cut when it is
missing."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.clips.trim import UNAVAILABLE_REASON, TrimBackend, TrimError


def test_available_is_false_when_no_ffmpeg_binary_is_configured_or_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nox.clips.trim.shutil.which", lambda _name: None)
    backend = TrimBackend(ffmpeg_path=str(tmp_path / "does_not_exist.exe"))
    assert backend.available() is False


def test_available_is_true_when_a_configured_path_exists(tmp_path: Path) -> None:
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"")
    backend = TrimBackend(ffmpeg_path=str(fake_ffmpeg))
    assert backend.available() is True


async def test_trim_raises_a_clear_error_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nox.clips.trim.shutil.which", lambda _name: None)
    backend = TrimBackend(ffmpeg_path=None)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"video")
    dst = tmp_path / "dst.mp4"
    with pytest.raises(TrimError, match="ffmpeg"):
        await backend.trim(src, dst, in_s=0.0, out_s=5.0)
    assert not dst.exists()
    assert UNAVAILABLE_REASON  # non-empty, used verbatim by the clip.trim tool
