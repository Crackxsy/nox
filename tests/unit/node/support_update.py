"""Shared helpers for the node-update tests: a signing key made per test, a small signed
release with its bundle, a fetcher that serves it from memory, and an updater wired to
them. Nothing here touches the network or an installed copy."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nox.node.bundle import BUNDLE_SCHEMA, bundle_name
from nox.node.update import NodeUpdater
from nox.release.keys import TrustedKey, describe_public_key
from nox.release.manifest import Asset, build_manifest

COMMIT = "0123456789abcdef0123456789abcdef01234567"


LOCK = "a" * 64


CREATED = dt.datetime(2026, 10, 21, tzinfo=dt.UTC)


class Key:
    """A release signing key made for one test, with its trusted-key entry."""

    def __init__(self, not_after: str = "2999-12-31") -> None:
        self.private = Ed25519PrivateKey.generate()
        key_id, spki = describe_public_key(self.private.public_key())
        self.trusted = TrustedKey(key_id=key_id, public_key=spki, not_after=not_after)

    def sign(self, data: bytes) -> bytes:
        return json.dumps(
            {
                "schema": "nox-release-signature/1",
                "algorithm": "ed25519",
                "key_id": self.trusted.key_id,
                "signature": base64.b64encode(self.private.sign(data)).decode(),
            }
        ).encode()


def make_bundle(
    version: str,
    *,
    lock: str = LOCK,
    protocol: int = 1,
    extra: dict[str, bytes] | None = None,
    listed: dict[str, bytes] | None = None,
    info: dict[str, Any] | None = None,
) -> bytes:
    """A small bundle: `extra` is in the zip but not listed, `listed` is listed and present."""
    files = {
        "nox/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "config/defaults.yaml": b"a: 1\n",
        **(listed or {}),
    }
    document = {
        "schema": BUNDLE_SCHEMA,
        "version": version,
        "node_protocol": protocol,
        "lock_sha256": lock,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        **(info or {}),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("BUNDLE.json", json.dumps(document))
        for name, data in {**files, **(extra or {})}.items():
            archive.writestr(name, data)
    return out.getvalue()


class Fetcher:
    """Serves a release from memory and records which files were asked for."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def get(self, path: str, limit: int) -> bytes:
        self.asked.append(path)
        data = self.files[path]
        assert len(data) <= limit
        return data

    def download(self, path: str, dest: Path, limit: int) -> None:
        self.asked.append(path)
        dest.write_bytes(self.files[path][: limit + 1])


def signed_release(  # noqa: PLR0913 - every knob a test turns
    key: Key,
    version: str,
    *,
    bundle: bytes | None = None,
    served: bytes | None = None,
    speaks: int | None = 1,
    deps_changed: bool | None = False,
    signer: Key | None = None,
    created: dt.datetime = CREATED,
) -> Fetcher:
    """A release of `version` signed with `key`, as a `Fetcher`; every knob breaks one check."""
    tag = f"v{version}"
    data = bundle if bundle is not None else make_bundle(version)
    asset = Asset(bundle_name(tag), len(data), hashlib.sha256(data).hexdigest())
    manifest = build_manifest(
        tag=tag,
        source_commit=COMMIT,
        assets=[asset],
        node_protocol=(1, 1),
        created=created,
        node_speaks=speaks,
        deps_changed=deps_changed,
    )
    return Fetcher(
        {
            "latest/download/release-manifest.json": manifest,
            "latest/download/release-manifest.json.sig": (signer or key).sign(manifest),
            f"download/{tag}/{bundle_name(tag)}": served if served is not None else data,
        }
    )


def smoke_reads_version(app: Path, _python: str) -> str:
    """The smoke test of an install: the version the installed package states."""
    return (app / "nox" / "__init__.py").read_text(encoding="utf-8").split('"')[1]


def updater(  # noqa: PLR0913 - every knob a test turns
    tmp_path: Path,
    fetcher: Fetcher,
    keys: list[Key],
    *,
    running: str = "0.5.0",
    core: tuple[int, int] | None = (1, 1),
    smoke: Callable[[Path, str], str] = smoke_reads_version,
    lock: str | None = LOCK,
    now: dt.datetime | None = None,
) -> NodeUpdater:
    """A `NodeUpdater` over `fetcher` that trusts `keys`, installing into a temporary folder."""
    return NodeUpdater(
        tmp_path / "app",
        fetcher=fetcher,
        trusted_keys=lambda: [k.trusted for k in keys],
        running=running,
        core_range=lambda: core,
        smoke=smoke,
        lock_sha256=lambda: lock,
        clock=lambda: now or dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
    )
