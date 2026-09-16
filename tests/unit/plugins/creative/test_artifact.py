"""`creative.artifact.inspect` metadata extraction (Spec v0.7 §3.3/§6, narrowed to file/header
metadata - see `artifact.py` module docstring). Sample fixtures are generated on the fly with
stdlib only, so these tests need no binary fixtures checked into the repo."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from nox_plugin_creative.artifact import inspect_artifact


def _write_wav(path: Path, *, seconds: float = 1.0, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))


def _write_minimal_png(path: Path, *, width: int = 4, height: int = 2) -> None:
    """A syntactically valid-enough PNG for `_png_dimensions`: signature + IHDR chunk. No IDAT/
    IEND needed since the header parser stops after IHDR."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # length + type + data (+ a CRC placeholder, unused by the parser)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    path.write_bytes(sig + ihdr)


class TestInspectArtifact:
    def test_missing_file_is_honest(self, tmp_path: Path) -> None:
        result = inspect_artifact(str(tmp_path / "nope.wav"))
        assert result["ok"] is False
        assert result["artefact_type"] == "unknown"
        assert "not found" in result["notes"][0]

    def test_wav_audio_metadata(self, tmp_path: Path) -> None:
        p = tmp_path / "export.wav"
        _write_wav(p, seconds=2.0, rate=44100)
        result = inspect_artifact(str(p))
        assert result["ok"] is True
        assert result["artefact_type"] == "audio"
        assert result["metadata"]["channels"] == 1
        assert result["metadata"]["sample_rate_hz"] == 44100
        assert abs(result["metadata"]["duration_s"] - 2.0) < 0.01
        assert result["confidence"] == "medium"

    def test_non_wav_audio_is_honest_without_mutagen(self, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "export.mp3"
        p.write_bytes(b"\x00" * 128)
        import nox_plugin_creative.artifact as artifact_mod

        monkeypatch.setattr(artifact_mod, "_optional_available", lambda _m: False)
        result = inspect_artifact(str(p))
        assert result["artefact_type"] == "audio"
        assert result["confidence"] == "low"
        assert any("mutagen" in n for n in result["notes"])
        assert "duration_s" not in result["metadata"]

    def test_png_dimensions_without_pillow(self, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "thumb.png"
        _write_minimal_png(p, width=64, height=32)
        import nox_plugin_creative.artifact as artifact_mod

        monkeypatch.setattr(artifact_mod, "_optional_available", lambda _m: False)
        result = inspect_artifact(str(p))
        assert result["artefact_type"] == "image"
        assert result["metadata"]["width"] == 64
        assert result["metadata"]["height"] == 32

    def test_unsupported_extension_is_honest(self, tmp_path: Path) -> None:
        p = tmp_path / "project.flp"
        p.write_bytes(b"whatever")
        result = inspect_artifact(str(p))
        assert result["ok"] is True
        assert result["artefact_type"] == "unsupported"
        assert result["confidence"] == "none"

    def test_blend_header_reports_version_never_scene_stats(self, tmp_path: Path) -> None:
        p = tmp_path / "scene.blend"
        # "BLENDER" + pointer-size(-) + endian(v=little) + 3-digit version, per the .blend spec.
        p.write_bytes(b"BLENDER-v302" + b"\x00" * 20)
        result = inspect_artifact(str(p))
        assert result["artefact_type"] == "blend"
        assert result["metadata"]["blender_version"] == "3.02"
        assert result["metadata"]["compressed"] is False
        assert any("Blender's Python API" in n for n in result["notes"])
        assert "object_count" not in result["metadata"]  # never a fabricated scene stat

    def test_gzip_compressed_blend_is_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "scene.blend"
        p.write_bytes(b"\x1f\x8b" + b"\x00" * 20)
        result = inspect_artifact(str(p))
        assert result["metadata"]["compressed"] is True
        assert result["confidence"] == "low"

    def test_video_without_ffprobe_is_honest(self, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 64)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        result = inspect_artifact(str(p))
        assert result["artefact_type"] == "video"
        assert result["confidence"] == "low"
        assert any("ffprobe" in n for n in result["notes"])
