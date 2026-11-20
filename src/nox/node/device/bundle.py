"""The node bundle `nox-app-<tag>.zip`: what it may contain, and how a folder unpacked from one is
checked again before anything in it runs.

`BUNDLE.json` inside the zip names the version, the node protocol that code speaks, the SHA-256
of the `uv.lock` it was built from (a node applies a bundle only on the same libraries) and the
SHA-256 of every file. A release folder a node installed itself keeps the zip, the signed
`release-manifest.json` and its signature next to the unpacked files, so the launcher can check,
at every start, with the keys of the *installed base* and nothing else:

1. the signature of the manifest (`nox.release.verify.verify_release`),
2. the folder's name is the manifest's version, and the zip is exactly the one the manifest lists,
3. every file in the folder is one `BUNDLE.json` lists, with that SHA-256 - and nothing else is
   there (bytecode caches are removed first; the node runs without writing new ones).

A node account that was taken over cannot leave code behind that survives this: a changed or
added file, a forged state file or a planted folder makes the launcher start the base instead.

Imports only the standard library and `nox.release` (cryptography), so the launcher can use it
from the base without loading the rest of Nox.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from nox.release.keys import TrustedKey
from nox.release.manifest import MANIFEST_NAME, SIGNATURE_NAME
from nox.release.verify import ReleaseVerificationError, verify_release

__all__ = [
    "ARCHIVE_NAME",
    "BUNDLE_SCHEMA",
    "MAX_ENTRIES",
    "MAX_UNPACKED_BYTES",
    "BundleError",
    "bundle_name",
    "read_info",
    "sha256_of",
    "unpack",
    "verify_folder",
]

BUNDLE_SCHEMA = "nox-app-bundle/1"
#: The bundle as downloaded, kept in the release folder for the check at every start.
ARCHIVE_NAME = "bundle.zip"
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 5000
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.+ -]*"
_ENTRY = re.compile(rf"^(?:BUNDLE\.json|(?:nox|config)/{_SEGMENT}(?:/{_SEGMENT})*)$")
#: Names Windows treats as devices whatever the extension, and names it silently changes.
_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$|[. ]$", re.IGNORECASE)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: Files of a release folder that are not part of the code.
_SIDECARS = {ARCHIVE_NAME, MANIFEST_NAME, SIGNATURE_NAME, "BUNDLE.json"}


class BundleError(ValueError):
    """The bundle or the folder is not what the signed release describes."""


def bundle_name(tag: str) -> str:
    return f"nox-app-{tag}.zip"


def sha256_of(handle: IO[bytes]) -> tuple[str, int]:
    """SHA-256 and size of what is left in `handle`, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _entry_ok(name: str) -> bool:
    return (
        bool(_ENTRY.match(name))
        and "/../" not in f"/{name}/"
        and not any(_WINDOWS_RESERVED.search(part) for part in name.split("/"))
    )


def read_info(bundle: zipfile.ZipFile) -> dict[str, Any]:
    """`BUNDLE.json` of an open bundle, checked for shape."""
    try:
        info = json.loads(bundle.read("BUNDLE.json").decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise BundleError("the bundle has no readable BUNDLE.json") from exc
    if not isinstance(info, dict) or info.get("schema") != BUNDLE_SCHEMA:
        raise BundleError("unknown bundle format")
    files = info.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and _entry_ok(k) and isinstance(v, str) and _HEX64.match(v)
        for k, v in files.items()
    ):
        raise BundleError("the bundle lists no valid files")
    for key in ("nox/__init__.py", "config/defaults.yaml"):
        if key not in files:
            raise BundleError("the bundle is incomplete")
    if not isinstance(info.get("version"), str) or not isinstance(info.get("lock_sha256"), str):
        raise BundleError("the bundle names no version or lock")
    if not isinstance(info.get("node_protocol"), int) or isinstance(info["node_protocol"], bool):
        raise BundleError("the bundle names no node protocol")
    return info


def unpack(bundle: zipfile.ZipFile, target: Path) -> dict[str, Any]:
    """Unpack into `target` (which must not exist): only listed, regular, bounded files, each with
    the SHA-256 `BUNDLE.json` names. Returns `BUNDLE.json`."""
    info = read_info(bundle)
    files: dict[str, str] = info["files"]
    entries = [e for e in bundle.infolist() if not e.is_dir()]
    if len(entries) > MAX_ENTRIES:
        raise BundleError("too many files in the bundle")
    if sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
        raise BundleError("the bundle unpacks too large")
    names = {e.filename for e in entries} - {"BUNDLE.json"}
    if names != set(files):
        raise BundleError("the bundle's files are not the ones BUNDLE.json lists")
    target.mkdir(parents=True)
    for entry in entries:
        mode = entry.external_attr >> 16
        if not _entry_ok(entry.filename) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise BundleError(f"unexpected entry {entry.filename[:80]!r}")
        destination = target / entry.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with bundle.open(entry) as source, destination.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > entry.file_size:
                    raise BundleError("an entry is larger than listed")
                digest.update(chunk)
                out.write(chunk)
        if entry.filename != "BUNDLE.json" and digest.hexdigest() != files[entry.filename]:
            raise BundleError(f"{entry.filename[:80]} does not match BUNDLE.json")
    return info


def _code_files(folder: Path) -> Iterable[Path]:
    for top in ("nox", "config"):
        base = folder / top
        if base.is_symlink():
            raise BundleError(f"{top} is a link")
        if base.exists():
            yield from base.rglob("*")


def verify_folder(folder: Path, trusted_keys: Sequence[TrustedKey]) -> dict[str, Any]:
    """Check a release folder a node installed itself; BundleError when anything differs from the
    signed release. Removes bytecode caches first."""
    if folder.is_symlink() or not folder.is_dir():
        raise BundleError("not a folder")
    try:
        release = verify_release(
            (folder / MANIFEST_NAME).read_bytes(),
            (folder / SIGNATURE_NAME).read_bytes(),
            trusted_keys=trusted_keys,
        )
    except OSError as exc:
        raise BundleError("the signed manifest is missing") from exc
    except ReleaseVerificationError as exc:
        raise BundleError(f"the signed manifest does not verify: {exc}") from exc
    manifest = release.manifest
    if folder.name != str(manifest.version):
        raise BundleError("the folder is not named after its signed version")
    asset = manifest.asset(bundle_name(manifest.tag))
    if asset is None:
        raise BundleError("the signed release lists no bundle")
    archive = folder / ARCHIVE_NAME
    if archive.is_symlink():
        raise BundleError("the bundle is a link")
    with archive.open("rb") as handle:
        digest, size = sha256_of(handle)
        if (digest, size) != (asset.sha256, asset.size):
            raise BundleError("the bundle is not the one the signed manifest lists")
        handle.seek(0)
        with zipfile.ZipFile(handle) as bundle:
            info = read_info(bundle)
            raw_info = bundle.read("BUNDLE.json")
    if info["version"] != str(manifest.version):
        raise BundleError("BUNDLE.json names another version")
    if (folder / "BUNDLE.json").read_bytes() != raw_info:
        raise BundleError("BUNDLE.json in the folder was changed")
    for cache in [p for p in _code_files(folder) if p.name == "__pycache__" and p.is_dir()]:
        shutil.rmtree(cache, ignore_errors=True)
    files: dict[str, str] = info["files"]
    seen: set[str] = set()
    for path in _code_files(folder):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            raise BundleError(f"{relative[:80]} is a link")
        if path.is_dir():
            continue
        expected = files.get(relative)
        if expected is None:
            raise BundleError(f"{relative[:80]} is not part of the release")
        with path.open("rb") as handle:
            if sha256_of(handle)[0] != expected:
                raise BundleError(f"{relative[:80]} was changed")
        seen.add(relative)
    if seen != set(files):
        raise BundleError("files of the release are missing")
    extra = {p.name for p in folder.iterdir()} - _SIDECARS - {"nox", "config"}
    if extra:
        raise BundleError(f"unexpected {sorted(extra)[0][:80]!r} in the release folder")
    return info
