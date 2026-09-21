"""`creative.artifact.inspect`: local, metadata-only inspection of an exported artefact (Creative
Apps,/04, narrowed for this pass to file/header metadata - no audio decode, no video decode, no
Blender subprocess).

Every extractor uses the stdlib first and an optional third-party library only if already
importable; a missing optional dependency is reported honestly as `"unavailable"` (the project
standards "no fake implementations" - never a fabricated number). Nothing here makes a network
call.
"""

from __future__ import annotations

import contextlib
import importlib.util
import struct
import wave
from pathlib import Path
from typing import Any

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}
BLEND_EXTENSIONS = {".blend"}


def _optional_available(module: str) -> bool:
    with contextlib.suppress(ImportError, ValueError):
        return importlib.util.find_spec(module) is not None
    return False


def _base_stat(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "size_bytes": stat.st_size, "extension": path.suffix.lower()}


def inspect_artifact(path: str) -> dict[str, Any]:
    """Dispatch by extension. Returns `{ok, artefact_type, metadata, confidence, notes}` - never
    raises for a missing/unreadable file, so a tool caller always gets an honest result."""
    p = Path(path)
    if not p.is_file():
        return {
            "ok": False,
            "artefact_type": "unknown",
            "metadata": {},
            "confidence": "none",
            "notes": [f"file not found: {path}"],
        }
    ext = p.suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return _inspect_audio(p)
    if ext in VIDEO_EXTENSIONS:
        return _inspect_video(p)
    if ext in IMAGE_EXTENSIONS:
        return _inspect_image(p)
    if ext in BLEND_EXTENSIONS:
        return _inspect_blend(p)
    return {
        "ok": True,
        "artefact_type": "unsupported",
        "metadata": _base_stat(p),
        "confidence": "none",
        "notes": [f"extension {ext!r} is not a recognised creative-artefact type"],
    }


# ---- audio -----------------------------------------------------------------------------------


def _inspect_audio(p: Path) -> dict[str, Any]:
    meta = _base_stat(p)
    notes: list[str] = []
    if p.suffix.lower() == ".wav":
        try:
            with wave.open(str(p), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                meta.update(
                    {
                        "channels": wav.getnchannels(),
                        "sample_rate_hz": rate,
                        "sample_width_bytes": wav.getsampwidth(),
                        "duration_s": round(frames / rate, 3) if rate else None,
                    }
                )
                confidence = "medium"
        except (wave.Error, EOFError, OSError) as exc:
            notes.append(f"could not parse WAV header: {exc}")
            confidence = "low"
    elif _optional_available("mutagen"):
        try:
            import mutagen

            audio = mutagen.File(str(p))
            if audio is not None and audio.info is not None:
                meta["duration_s"] = round(float(audio.info.length), 3)
                meta["bitrate_bps"] = getattr(audio.info, "bitrate", None)
                confidence = "medium"
            else:
                notes.append("mutagen could not parse this file")
                confidence = "low"
        except Exception as exc:  # noqa: BLE001 - optional-lib parsing must never crash the tool
            notes.append(f"mutagen parse failed: {exc}")
            confidence = "low"
    else:
        notes.append(
            "detailed audio metadata unavailable: optional 'mutagen' package not installed "
            "(only file size is reported for non-WAV audio)"
        )
        confidence = "low"
    notes.append(
        "loudness/mix/structure analysis is out of scope for this pass: metadata-only artefact "
        "inspection, because decoding the file has no resource budget here yet"
    )
    return {
        "ok": True,
        "artefact_type": "audio",
        "metadata": meta,
        "confidence": confidence,
        "notes": notes,
    }


# ---- video -----------------------------------------------------------------------------------


def _inspect_video(p: Path) -> dict[str, Any]:
    import json
    import shutil
    import subprocess

    meta = _base_stat(p)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {
            "ok": True,
            "artefact_type": "video",
            "metadata": meta,
            "confidence": "low",
            "notes": [
                "detailed video metadata unavailable: 'ffprobe' not found on PATH (only file size "
                "is reported)",
                "cut-rhythm/color/audio-sync analysis is out of scope for this pass (metadata-only "
                "artefact inspection)",
            ],
        }
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, local read-only probe, no shell
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(p),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {
            "ok": True,
            "artefact_type": "video",
            "metadata": meta,
            "confidence": "low",
            "notes": [f"ffprobe failed: {exc}"],
        }
    fmt = data.get("format", {})
    video_stream: dict[str, Any] = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    meta.update(
        {
            "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "codec": video_stream.get("codec_name"),
        }
    )
    return {
        "ok": True,
        "artefact_type": "video",
        "metadata": meta,
        "confidence": "medium",
        "notes": [
            "cut-rhythm/color/audio-sync analysis is out of scope for this pass: metadata-only "
            "artefact inspection, because decoding the file has no resource budget here yet"
        ],
    }


# ---- image -----------------------------------------------------------------------------------


def _inspect_image(p: Path) -> dict[str, Any]:
    meta = _base_stat(p)
    notes: list[str] = []
    width = height = None
    if _optional_available("PIL"):
        try:
            from PIL import Image

            with Image.open(p) as img:
                width, height = img.size
                meta["mode"] = img.mode
            confidence = "medium"
        except Exception as exc:  # noqa: BLE001 - Pillow parse failure must not crash the tool
            notes.append(f"Pillow could not parse this file: {exc}")
            confidence = "low"
    elif p.suffix.lower() == ".png":
        dims = _png_dimensions(p)
        if dims:
            width, height = dims
            confidence = "medium"
        else:
            notes.append("could not parse PNG header")
            confidence = "low"
    else:
        notes.append(
            "detailed image metadata unavailable: optional 'Pillow' package not installed "
            "(only file size is reported for non-PNG images)"
        )
        confidence = "low"
    if width is not None:
        meta["width"] = width
        meta["height"] = height
    notes.append(
        "composition/color-balance analysis is out of scope for this pass (metadata-only artefact "
        "inspection)"
    )
    return {
        "ok": True,
        "artefact_type": "image",
        "metadata": meta,
        "confidence": confidence,
        "notes": notes,
    }


def _png_dimensions(p: Path) -> tuple[int, int] | None:
    """Minimal PNG IHDR parse (stdlib only): magic bytes + big-endian width/height at fixed
    offsets - https://www.w3.org/TR/png/#11IHDR."""
    try:
        with p.open("rb") as f:
            header = f.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return width, height


# ---- blend -----------------------------------------------------------------------------------


def _inspect_blend(p: Path) -> dict[str, Any]:
    """Header-only, read-only `.blend` inspection: magic bytes plus the declared Blender version.

    Blender itself is never started. Object, material, light and scene counts would need Blender's
    own Python API, which this deliberately does not reach for - an honest "not inspected" beats
    fabricated scene statistics.
    """
    meta = _base_stat(p)
    notes = [
        "scene/object/material/light/render-settings stats require Blender's Python API "
        "(not implemented in this pass - file-header metadata only)"
    ]
    try:
        with p.open("rb") as f:
            head = f.read(12)
    except OSError as exc:
        notes.append(f"could not read file: {exc}")
        return {
            "ok": True,
            "artefact_type": "blend",
            "metadata": meta,
            "confidence": "none",
            "notes": notes,
        }
    if head[:2] == b"\x1f\x8b":
        meta["compressed"] = True
        notes.append("file is gzip-compressed; version header not read without decompressing")
        confidence = "low"
    elif head[:7] == b"BLENDER":
        meta["compressed"] = False
        pointer_size = "64-bit" if head[7:8] == b"-" else "32-bit"
        endian = "little" if head[8:9] == b"v" else "big"
        version_raw = head[9:12].decode("ascii", errors="replace")
        meta.update(
            {
                "pointer_size": pointer_size,
                "endian": endian,
                "blender_version": f"{version_raw[0]}.{version_raw[1:]}"
                if len(version_raw) == 3
                else version_raw,
            }
        )
        confidence = "low"
    else:
        notes.append("file does not start with a recognised .blend header")
        confidence = "none"
    return {
        "ok": True,
        "artefact_type": "blend",
        "metadata": meta,
        "confidence": confidence,
        "notes": notes,
    }
