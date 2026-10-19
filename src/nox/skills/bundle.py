"""Handing a skill's files to a sandboxed process over stdin, instead of sharing a folder.

A sandboxed process can read and write exactly one folder: the private work folder the sandbox
creates for it (`Sandbox.start(..., cwd=None)`). Nothing under `<nox home>` - where the skill is
kept - is ever made readable to it. So the core reads the skill's checked files itself and sends
them as the first stdin line; `BOOTSTRAP` (run as `python -I -c BOOTSTRAP <entry>`) writes them
into the work folder, closes nothing it was not given, and then runs the trusted entry script
(the test runner, or the worker shim) that is part of the same bundle.

The bundle is bounded by the static checks (`nox.skills.checks`: at most 60 files, 512 KiB) and
every path is relative, without `..` - checked here when it is built and again by the bootstrap.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

__all__ = ["BOOTSTRAP", "MAX_BUNDLE_BYTES", "BundleError", "bundle_files", "encode_bundle"]

MAX_BUNDLE_BYTES = 2 * 1024 * 1024

#: Runs first inside the sandbox: unpack the bundle from stdin into the work folder, then run the
#: entry script named on the command line as `__main__`.
BOOTSTRAP = (
    "import json, pathlib, runpy, sys\n"
    "bundle = json.loads(sys.stdin.buffer.readline())\n"
    "for rel, text in bundle.items():\n"
    "    path = pathlib.PurePosixPath(rel)\n"
    "    if path.is_absolute() or '..' in path.parts or ':' in rel:\n"
    "        raise SystemExit(2)\n"
    "    target = pathlib.Path(*path.parts)\n"
    "    target.parent.mkdir(parents=True, exist_ok=True)\n"
    "    target.write_text(text, encoding='utf-8')\n"
    "del bundle\n"
    "runpy.run_path(sys.argv[1], run_name='__main__')\n"
)


class BundleError(ValueError):
    """The folder cannot be sent: a path escapes it, a file is not text, or it is too large."""


def _safe(rel: str) -> bool:
    path = PurePosixPath(rel)
    return bool(rel) and not path.is_absolute() and ".." not in path.parts and ":" not in rel


def bundle_files(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Every text file under `root` (no `__pycache__`, no hidden files) plus `extra`."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in PurePosixPath(rel).parts):
            continue
        if not _safe(rel):
            raise BundleError(f"unsafe path {rel!r}")
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BundleError(f"{rel} is not UTF-8 text") from exc
    for rel, text in (extra or {}).items():
        if not _safe(rel):
            raise BundleError(f"unsafe path {rel!r}")
        files[rel] = text
    return files


def encode_bundle(files: dict[str, str]) -> bytes:
    """The first stdin line for `BOOTSTRAP`; refuses a bundle over `MAX_BUNDLE_BYTES`."""
    data = json.dumps(files, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"{len(data)} bytes; at most {MAX_BUNDLE_BYTES}")
    return data
