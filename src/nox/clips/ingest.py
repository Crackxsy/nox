"""Shared "a clip file appeared" ingestion path (Spec v0.6 §6.3): used by both
`ClipCaptureService` (event-triggered/manual path, file resolved by `obs.replay_buffer.save`) and
`ClipWatcher` (files that show up in the watch folder without a `clip.requested`). Always copies
into `config.clips.library_root` - the OBS output/watch folder and the recording root are read-only
for this pipeline; the source file is never moved, renamed or deleted."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from nox.clips.media import make_thumbnail, probe_duration_s, sha256_file
from nox.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestedClip:
    file_path: Path
    checksum: str
    duration_s: float
    thumbnail_path: Path | None


def ingest_file(src: Path, *, library_root: Path) -> IngestedClip:
    """Copy `src` into `library_root` (never touching `src`), compute its checksum/duration, and
    generate a thumbnail when opencv is available. Blocking - callers run this in a thread."""
    library_root.mkdir(parents=True, exist_ok=True)
    dest = library_root / src.name
    if dest.exists() and dest.resolve() != src.resolve():
        dest = library_root / f"{src.stem}_{sha256_file(src)[:8]}{src.suffix}"
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    checksum = sha256_file(dest)
    duration = probe_duration_s(dest)
    thumb_dir = library_root / "thumbnails"
    thumbnail = make_thumbnail(dest, thumb_dir / f"{dest.stem}.jpg")
    return IngestedClip(
        file_path=dest, checksum=checksum, duration_s=duration, thumbnail_path=thumbnail
    )
