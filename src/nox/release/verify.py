"""Checking a release before anything of it is used. Fail closed: every doubt is a refusal.

`verify_release` accepts a manifest only when

1. at least one release key is trusted (otherwise `SigningNotConfiguredError` - a distinct
   outcome, so a caller can say "not set up yet" instead of "verified");
2. the detached signature names a trusted key that has not expired, and is a valid Ed25519
   signature by that key over the manifest's exact bytes;
3. the manifest is well-formed, names this project, and - when the caller knows them - the tag it
   asked for and the commit that tag points to;
4. its version is not lower than the lowest one the caller still accepts (**rollback
   protection**: an old, validly signed release with known holes cannot be served as an update),
   unless the owner asked for that downgrade explicitly.

Only then does `verify_file` mean anything: it compares a downloaded file with the size and
SHA-256 the signed manifest lists for it.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature

from nox.release.keys import KEY_ID_PATTERN, TrustedKey
from nox.release.manifest import (
    MAX_MANIFEST_BYTES,
    PROJECT,
    ManifestError,
    ReleaseManifest,
    Version,
    parse_manifest,
)

__all__ = [
    "MAX_SIGNATURE_BYTES",
    "SIGNATURE_SCHEMA",
    "ReleaseVerificationError",
    "SigningNotConfiguredError",
    "VerifiedRelease",
    "verify_file",
    "verify_release",
]

SIGNATURE_SCHEMA = "nox-release-signature/1"
MAX_SIGNATURE_BYTES = 4096


class ReleaseVerificationError(Exception):
    """The release is refused. `code` is stable for scripts; the message is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SigningNotConfiguredError(ReleaseVerificationError):
    """No release key is trusted, so there is nothing to verify against."""

    def __init__(self) -> None:
        super().__init__(
            "not_configured",
            "release signing is not configured in this version of Nox yet (no trusted release "
            "key) - see docs/RELEASES.md",
        )


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    manifest: ReleaseManifest
    key_id: str


def _signature(data: bytes) -> tuple[str, bytes]:
    if len(data) > MAX_SIGNATURE_BYTES:
        raise ReleaseVerificationError("bad_signature", "the signature file is far too large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("bad_signature", "the signature file is not JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != SIGNATURE_SCHEMA:
        raise ReleaseVerificationError("bad_signature", "unknown signature format")
    if document.get("algorithm") != "ed25519":
        raise ReleaseVerificationError("bad_signature", "the signature is not Ed25519")
    key_id = document.get("key_id")
    if not isinstance(key_id, str) or not KEY_ID_PATTERN.match(key_id):
        raise ReleaseVerificationError("bad_signature", "the signature names no valid key id")
    raw = document.get("signature")
    try:
        signature = base64.b64decode(raw if isinstance(raw, str) else "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseVerificationError("bad_signature", "the signature is not base64") from exc
    if len(signature) != 64:
        raise ReleaseVerificationError("bad_signature", "an Ed25519 signature has 64 bytes")
    return key_id, signature


def verify_release(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    *,
    trusted_keys: Sequence[TrustedKey],
    today: dt.date | None = None,
    expect_tag: str | None = None,
    expect_commit: str | None = None,
    min_version: str | None = None,
    allow_downgrade: bool = False,
) -> VerifiedRelease:
    """The verified manifest, or ReleaseVerificationError (SigningNotConfiguredError when no
    key is trusted)."""
    if not trusted_keys:
        raise SigningNotConfiguredError()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ReleaseVerificationError("bad_manifest", "the manifest is far too large")
    key_id, signature = _signature(signature_bytes)
    key = next((candidate for candidate in trusted_keys if candidate.key_id == key_id), None)
    if key is None:
        raise ReleaseVerificationError(
            "unknown_key", f"signed by key {key_id}, which this version of Nox does not trust"
        )
    day = today or dt.datetime.now(dt.UTC).date()
    if not key.valid_on(day):
        raise ReleaseVerificationError(
            "expired_key", f"signed by key {key_id}, which expired on {key.not_after}"
        )
    try:
        key.load().verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise ReleaseVerificationError(
            "bad_signature", f"the signature does not match the manifest (key {key_id})"
        ) from exc

    # Parsed only after the signature holds: nothing unauthenticated reaches the parser's logic.
    try:
        manifest = parse_manifest(manifest_bytes)
    except ManifestError as exc:
        raise ReleaseVerificationError("bad_manifest", f"signed manifest refused: {exc}") from exc
    if manifest.project != PROJECT:
        raise ReleaseVerificationError(
            "wrong_project", f"the manifest is for {manifest.project[:60]!r}, not {PROJECT}"
        )
    if expect_tag is not None and manifest.tag != expect_tag:
        raise ReleaseVerificationError(
            "wrong_tag", f"the manifest is for {manifest.tag}, not for {expect_tag}"
        )
    if expect_commit is not None and manifest.source_commit != expect_commit.lower():
        raise ReleaseVerificationError(
            "wrong_commit",
            f"{manifest.tag} points to commit {expect_commit[:12]}, but its signed manifest "
            f"says {manifest.source_commit[:12]}",
        )
    if min_version is not None and not allow_downgrade:
        try:
            floor = Version.parse(min_version.removeprefix("v"))
        except ManifestError as exc:
            raise ReleaseVerificationError("bad_floor", f"minimum version: {exc}") from exc
        if manifest.version < floor:
            raise ReleaseVerificationError(
                "downgrade",
                f"{manifest.tag} is older than {floor}, the newest version this installation "
                "has accepted (rollback protection); a deliberate downgrade needs "
                "--allow-downgrade",
            )
    return VerifiedRelease(manifest=manifest, key_id=key_id)


def verify_file(release: VerifiedRelease, path: Path, *, name: str | None = None) -> None:
    """The file must be listed in the verified manifest with exactly this size and SHA-256."""
    asset_name = name or path.name
    asset = release.manifest.asset(asset_name)
    if asset is None:
        raise ReleaseVerificationError(
            "not_listed", f"{asset_name} is not part of release {release.manifest.tag}"
        )
    size = path.stat().st_size
    if size != asset.size:
        raise ReleaseVerificationError(
            "mismatch", f"{asset_name} has {size} bytes, the signed manifest says {asset.size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != asset.sha256:
        raise ReleaseVerificationError(
            "mismatch", f"{asset_name} does not match the SHA-256 in the signed manifest"
        )
