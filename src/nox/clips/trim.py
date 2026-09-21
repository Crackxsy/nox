"""Trim backend: ffmpeg is not currently installed, so `TrimBackend.available` probes
`shutil.which("ffmpeg")` at call time rather than assuming a bundled binary (the project standards
"no fake implementations" - an unavailable backend must fail honestly, never fake a trim or
silently drop the file)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from nox.core.logging import get_logger

log = get_logger(__name__)

UNAVAILABLE_REASON = (
    "no cutting backend available: ffmpeg is not installed and no OBS-side export fallback is "
    "configured"
)


class TrimError(RuntimeError):
    """ffmpeg ran but failed (non-zero exit)."""


class TrimBackend:
    """Wraps an external `ffmpeg` binary. Every call is a subprocess - no library binding, no
    format assumptions beyond stream copy (`-c copy`) so re-encoding never silently changes
    quality."""

    def __init__(self, *, ffmpeg_path: str | None = None) -> None:
        self._configured_path = ffmpeg_path

    def available(self) -> bool:
        return self._resolve() is not None

    def _resolve(self) -> str | None:
        if self._configured_path and Path(self._configured_path).is_file():
            return self._configured_path
        return shutil.which("ffmpeg")

    async def trim(self, src: Path, dst: Path, *, in_s: float, out_s: float) -> None:
        """Write `dst` as `src[in_s:out_s]`. Never touches `src`. Raises `TrimError` on a non-zero
        ffmpeg exit; callers should check `available` first for the "no backend" case, which is
        not an error but a `{"ok": False, "reason":...}` tool result."""
        ffmpeg = self._resolve()
        if ffmpeg is None:
            raise TrimError(UNAVAILABLE_REASON)
        dst.parent.mkdir(parents=True, exist_ok=True)
        duration = max(0.0, out_s - in_s)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-ss",
            f"{in_s:.3f}",
            "-i",
            str(src),
            "-t",
            f"{duration:.3f}",
            "-c",
            "copy",
            str(dst),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="replace")[:400]
            raise TrimError(f"ffmpeg exited {proc.returncode}: {detail}")
