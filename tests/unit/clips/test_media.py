"""`nox.clips.media`: checksum is always available; duration/thumbnail degrade honestly (`0.0`/
`None`) when opencv is not installed or the file is not a real video, rather than faking a value
(ENGINEERING.md "no fake implementations")."""

from __future__ import annotations

from pathlib import Path

from nox.clips.media import make_thumbnail, opencv_available, probe_duration_s, sha256_file


def test_sha256_file_is_deterministic(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world")
    assert sha256_file(f) == sha256_file(f)
    g = tmp_path / "b.bin"
    g.write_bytes(b"different")
    assert sha256_file(f) != sha256_file(g)


def test_probe_duration_on_a_non_video_file_is_honestly_zero(tmp_path: Path) -> None:
    f = tmp_path / "not_a_video.mp4"
    f.write_bytes(b"not really a video")
    assert probe_duration_s(f) == 0.0


def test_make_thumbnail_on_a_non_video_file_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "not_a_video.mp4"
    f.write_bytes(b"not really a video")
    dest = tmp_path / "thumb.jpg"
    assert make_thumbnail(f, dest) is None
    assert not dest.exists()


def test_opencv_available_is_a_bool() -> None:
    assert isinstance(opencv_available(), bool)
