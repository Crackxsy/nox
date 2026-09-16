"""Media helpers for the Clip Pipeline (ST-15-04, Spec v0.6 §7): checksum for dedup/integrity,
duration probe and thumbnail generation. opencv is optional (ENGINEERING.md "no fake
implementations") - `probe_duration_s`/`make_thumbnail` degrade to an honest "unavailable" (`0.0`/
`None`) instead of faking a value when it is not installed."""

from __future__ import annotations

import hashlib
from pathlib import Path

from nox.core.logging import get_logger

log = get_logger(__name__)

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 over `path`; never loads the whole file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401, PLC0415 - optional dependency, probed at call time
    except ImportError:
        return False
    return True


def probe_duration_s(path: Path) -> float:
    """Video duration via opencv (frame_count / fps). `0.0` when opencv is missing or the file
    cannot be read - never a fabricated number."""
    try:
        import cv2  # noqa: PLC0415 - optional dependency
    except ImportError:
        return 0.0
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return 0.0
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if fps <= 0:
            return 0.0
        return float(frames / fps)
    except (OSError, cv2.error) as exc:
        log.warning("clips.duration_probe_failed", error=str(exc))
        return 0.0
    finally:
        cap.release()


def make_thumbnail(path: Path, dest: Path) -> Path | None:
    """First-frame JPEG thumbnail via opencv. `None` when opencv is missing or extraction fails -
    the dashboard shows no thumbnail rather than a broken/fake one."""
    try:
        import cv2  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(dest), frame):
            return None
        return dest
    except (OSError, cv2.error) as exc:
        log.warning("clips.thumbnail_failed", error=str(exc))
        return None
    finally:
        cap.release()
