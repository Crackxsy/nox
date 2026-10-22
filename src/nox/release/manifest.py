"""The release manifest: what one release consists of, in a form a signature can vouch for.

`release-manifest.json` lists the release's version and tag, the git commit its source is taken
from, and every published file with its size and SHA-256. It is signed as it is - the exact bytes
of the file, no re-serialisation - and the detached signature lives next to it in
`release-manifest.json.sig`. The bash installer checks the same bytes with `openssl`, so the format
needs no canonical-JSON rules on the verifying side; `build_manifest` still writes it
deterministically (sorted keys, fixed indentation) so two builds of one release are identical.

Fields beyond the ones parsed here are ignored, so a later version may add some without locking
out installations that verify with this one. The fields here are typed strictly: a manifest that
gets one of them wrong is refused, not guessed at.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.release.keys import KEY_ID_PATTERN, TrustedKey

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "MAX_MANIFEST_BYTES",
    "PROJECT",
    "SIGNATURE_NAME",
    "Asset",
    "ManifestError",
    "ReleaseManifest",
    "Version",
    "asset_for_file",
    "build_manifest",
    "parse_manifest",
]

MANIFEST_SCHEMA = "nox-release-manifest/1"
MANIFEST_NAME = "release-manifest.json"
SIGNATURE_NAME = "release-manifest.json.sig"
#: The repository a manifest must name: a key that ever signed something else cannot be replayed.
PROJECT = "Crackxsy/nox"
#: Far above any real manifest (a few KiB); keeps a hostile download from exhausting memory.
MAX_MANIFEST_BYTES = 256 * 1024

_VERSION = re.compile(
    r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
#: A plain file name: no folders, no `..`, nothing a shell or a URL would treat specially.
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_CHANNELS = frozenset({"stable", "prerelease"})


class ManifestError(ValueError):
    """The manifest is malformed. The message says which field, never echoes large input."""


@dataclass(frozen=True, slots=True, order=False)
class Version:
    """A release version `X.Y.Z` or `X.Y.Z-pre.release`, ordered by Semantic Versioning: a
    pre-release sorts below its release (`0.7.0-rc1 < 0.7.0`), numeric identifiers numerically."""

    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _VERSION.match(text)
        if match is None:
            raise ManifestError(f"not a release version: {text[:40]!r}")
        major, minor, patch, pre = match.groups()
        return cls(int(major), int(minor), int(patch), tuple(pre.split(".")) if pre else ())

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{'.'.join(self.pre)}" if self.pre else core

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self) -> tuple[Any, ...]:
        # No pre-release outranks any pre-release of the same X.Y.Z. Within a pre-release,
        # numeric identifiers sort below alphanumeric ones and numerically among themselves.
        pre: tuple[tuple[int, int | str], ...] = tuple(
            (0, int(part)) if part.isdigit() else (1, part) for part in self.pre
        )
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, pre)

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()

    def __le__(self, other: Version) -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: Version) -> bool:
        return self._key() > other._key()

    def __ge__(self, other: Version) -> bool:
        return self._key() >= other._key()


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: Version
    tag: str
    channel: str
    created: str
    source_commit: str
    assets: tuple[Asset, ...]
    next_keys: tuple[TrustedKey, ...] = ()
    node_protocol: tuple[int, int] | None = None
    min_node_version: Version | None = None
    #: The node protocol the `nox node` of this release speaks (C7): a node installs it by itself
    #: only when its paired core accepts that protocol.
    node_speaks: int | None = None
    #: Whether `uv.lock` changed since the previous release: then a node's code-only update
    #: cannot apply and the installer is needed.
    deps_changed: bool | None = None
    project: str = PROJECT
    extra: Mapping[str, Any] = field(default_factory=dict)

    def asset(self, name: str) -> Asset | None:
        return next((asset for asset in self.assets if asset.name == name), None)


def asset_for_file(path: Path, *, name: str | None = None) -> Asset:
    """Size and SHA-256 of one file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    asset = Asset(name=name or path.name, size=size, sha256=digest.hexdigest())
    _check_asset_name(asset.name)
    return asset


def _check_asset_name(name: str) -> None:
    if not _ASSET_NAME.match(name) or ".." in name:
        raise ManifestError(f"asset name {name[:60]!r} is not a plain file name")


def build_manifest(
    *,
    tag: str,
    source_commit: str,
    assets: Sequence[Asset],
    channel: str | None = None,
    created: dt.datetime | None = None,
    next_keys: Iterable[TrustedKey] = (),
    node_protocol: tuple[int, int] | None = None,
    min_node_version: str | None = None,
    node_speaks: int | None = None,
    deps_changed: bool | None = None,
) -> bytes:
    """The manifest file's bytes. Validated by parsing them back, so nothing is signed that a
    verifier would refuse."""
    if not tag.startswith("v"):
        raise ManifestError(f"a release tag starts with v: {tag[:40]!r}")
    version = Version.parse(tag[1:])
    stamp = (created or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    document: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "project": PROJECT,
        "version": str(version),
        "tag": tag,
        "channel": channel or ("prerelease" if version.is_prerelease else "stable"),
        "created": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": source_commit,
        "node_protocol": (
            None if node_protocol is None else {"min": node_protocol[0], "max": node_protocol[1]}
        ),
        "min_node_version": min_node_version,
        "next_keys": [
            {"key_id": key.key_id, "public_key": key.public_key, "not_after": key.not_after}
            for key in next_keys
        ],
        "assets": [
            {"name": asset.name, "size": asset.size, "sha256": asset.sha256}
            for asset in sorted(assets, key=lambda item: item.name)
        ],
    }
    # Written only when known, so a manifest without them keeps its earlier layout.
    if node_speaks is not None:
        document["node_speaks"] = node_speaks
    if deps_changed is not None:
        document["deps_changed"] = deps_changed
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    parse_manifest(data)
    return data


def _require(document: Mapping[str, Any], key: str, kind: type) -> Any:
    value = document.get(key)
    # bool is an int in Python; a size or version number must never be `true`.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ManifestError(f"field {key!r} is missing or not a {kind.__name__}")
    return value


def parse_manifest(data: bytes) -> ReleaseManifest:
    """Parse and validate the manifest's bytes. Raises ManifestError."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is larger than any release manifest")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ManifestError("manifest is not a JSON object")
    if document.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"unknown manifest schema (expected {MANIFEST_SCHEMA})")

    project = _require(document, "project", str)
    version = Version.parse(_require(document, "version", str))
    tag = _require(document, "tag", str)
    if tag != f"v{version}":
        raise ManifestError(f"tag {tag[:40]!r} does not match version {version}")
    channel = _require(document, "channel", str)
    if channel not in _CHANNELS:
        raise ManifestError(f"unknown channel {channel[:20]!r}")
    if (channel == "prerelease") != version.is_prerelease:
        raise ManifestError(f"channel {channel} does not fit version {version}")
    created = _require(document, "created", str)
    try:
        dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ManifestError("field 'created' is not an ISO UTC timestamp") from exc
    commit = _require(document, "source_commit", str)
    if not _COMMIT.match(commit):
        raise ManifestError("field 'source_commit' is not a full git commit id")

    assets: list[Asset] = []
    for entry in _require(document, "assets", list):
        if not isinstance(entry, dict):
            raise ManifestError("an asset is not a JSON object")
        asset = Asset(
            name=_require(entry, "name", str),
            size=_require(entry, "size", int),
            sha256=_require(entry, "sha256", str),
        )
        _check_asset_name(asset.name)
        if asset.size < 0 or not _SHA256.match(asset.sha256):
            raise ManifestError(f"asset {asset.name} has an invalid size or SHA-256")
        assets.append(asset)
    if len({asset.name for asset in assets}) != len(assets):
        raise ManifestError("an asset is listed twice")

    next_keys: list[TrustedKey] = []
    for entry in _require(document, "next_keys", list):
        if not isinstance(entry, dict):
            raise ManifestError("a next key is not a JSON object")
        key_id = _require(entry, "key_id", str)
        if not KEY_ID_PATTERN.match(key_id):
            raise ManifestError("a next key has an invalid key id")
        try:
            next_keys.append(
                TrustedKey(
                    key_id=key_id,
                    public_key=_require(entry, "public_key", str),
                    not_after=_require(entry, "not_after", str),
                )
            )
        except ValueError as exc:
            raise ManifestError(f"next key {key_id}: {exc}") from exc

    protocol: tuple[int, int] | None = None
    raw_protocol = document.get("node_protocol")
    if raw_protocol is not None:
        if not isinstance(raw_protocol, dict):
            raise ManifestError("field 'node_protocol' is not an object")
        low, high = _require(raw_protocol, "min", int), _require(raw_protocol, "max", int)
        if not 0 < low <= high:
            raise ManifestError("field 'node_protocol' is not a range min..max")
        protocol = (low, high)
    raw_min_node = document.get("min_node_version")
    min_node = (
        None if raw_min_node is None else Version.parse(_require(document, "min_node_version", str))
    )

    speaks: int | None = None
    if document.get("node_speaks") is not None:
        speaks = _require(document, "node_speaks", int)
        if not 0 < speaks <= 1000:
            raise ManifestError("field 'node_speaks' is not a protocol number")
    deps_changed: bool | None = None
    if document.get("deps_changed") is not None:
        deps_changed = _require(document, "deps_changed", bool)

    known = {
        "schema",
        "project",
        "version",
        "tag",
        "channel",
        "created",
        "source_commit",
        "assets",
        "next_keys",
        "node_protocol",
        "min_node_version",
        "node_speaks",
        "deps_changed",
    }
    return ReleaseManifest(
        version=version,
        tag=tag,
        channel=channel,
        created=created,
        source_commit=commit,
        assets=tuple(assets),
        next_keys=tuple(next_keys),
        node_protocol=protocol,
        min_node_version=min_node,
        node_speaks=speaks,
        deps_changed=deps_changed,
        project=project,
        extra={key: value for key, value in document.items() if key not in known},
    )
