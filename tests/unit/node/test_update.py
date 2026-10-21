"""A node updates itself only from a signed release: every check of `nox.node.update`, in the
order it runs, and the switch and rollback around it. Keys are generated per test and never
stored; nothing here touches the network or an installed copy."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nox.node.update import BUNDLE_SCHEMA, NodeUpdater, UpdateState, bundle_name
from nox.release.keys import TrustedKey, describe_public_key
from nox.release.manifest import Asset, build_manifest

REPO = Path(__file__).resolve().parents[3]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class Key:
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
    packages: dict[str, list[str]] | None = None,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        info = {"schema": BUNDLE_SCHEMA, "version": version, "packages": packages or {}}
        bundle.writestr("BUNDLE.json", json.dumps({**info, "requires": []}))
        bundle.writestr("nox/__init__.py", f'__version__ = "{version}"\n')
        bundle.writestr("config/defaults.yaml", "a: 1\n")
        for name, data in (extra or {}).items():
            bundle.writestr(name, data)
    return out.getvalue()


class Fetcher:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def get(self, name: str, limit: int) -> bytes:
        self.asked.append(name)
        data = self.files[name]
        assert len(data) <= limit
        return data

    def download(self, name: str, dest: Path, limit: int) -> None:
        self.asked.append(name)
        dest.write_bytes(self.files[name][: limit + 1])


def release(
    key: Key,
    version: str,
    *,
    bundle: bytes | None = None,
    served: bytes | None = None,
    protocol: tuple[int, int] | None = (1, 1),
    signer: Key | None = None,
) -> Fetcher:
    tag = f"v{version}"
    data = bundle if bundle is not None else make_bundle(version)
    asset = Asset(bundle_name(tag), len(data), hashlib.sha256(data).hexdigest())
    manifest = build_manifest(
        tag=tag,
        source_commit=COMMIT,
        assets=[asset],
        node_protocol=protocol,
        created=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
    )
    return Fetcher(
        {
            "release-manifest.json": manifest,
            "release-manifest.json.sig": (signer or key).sign(manifest),
            bundle_name(tag): served if served is not None else data,
        }
    )


def smoke_reads_version(app: Path, _python: str) -> str:
    text = (app / "nox" / "__init__.py").read_text(encoding="utf-8")
    return text.split('"')[1]


def updater(
    tmp_path: Path,
    fetcher: Fetcher,
    keys: list[Key],
    *,
    running: str = "0.5.0",
    smoke: Callable[[Path, str], str] = smoke_reads_version,
    installed: Callable[[str], str | None] = lambda _name: None,
) -> NodeUpdater:
    return NodeUpdater(
        tmp_path / "app",
        fetcher=fetcher,
        trusted_keys=lambda: [k.trusted for k in keys],
        running=running,
        smoke=smoke,
        installed=installed,
    )


# ---- 1. no trusted key: nothing is fetched, nothing installed ----------------------------------


def test_without_a_trusted_key_no_update_is_even_looked_at(tmp_path: Path) -> None:
    fetcher = release(Key(), "0.5.1")
    check = updater(tmp_path, fetcher, []).check()
    assert check.status == "not_configured"
    assert "not configured" in check.message
    assert fetcher.asked == []
    assert not (tmp_path / "app").exists()


# ---- 2. the signature ------------------------------------------------------------------------


def test_a_bad_signature_is_refused(tmp_path: Path) -> None:
    key = Key()
    fetcher = release(key, "0.5.1")
    fetcher.files["release-manifest.json.sig"] = key.sign(b"another manifest")
    check = updater(tmp_path, fetcher, [key]).check()
    assert check.status == "refused" and "bad_signature" in check.message


def test_a_manifest_signed_by_an_unknown_key_is_refused(tmp_path: Path) -> None:
    trusted, stranger = Key(), Key()
    check = updater(tmp_path, release(trusted, "0.5.1", signer=stranger), [trusted]).check()
    assert check.status == "refused" and "unknown_key" in check.message


def test_a_manifest_signed_by_an_expired_key_is_refused(tmp_path: Path) -> None:
    old = Key(not_after="2001-01-01")
    check = updater(tmp_path, release(old, "0.5.1"), [old]).check()
    assert check.status == "refused" and "expired_key" in check.message


def test_a_changed_manifest_breaks_the_signature(tmp_path: Path) -> None:
    key = Key()
    fetcher = release(key, "0.5.1")
    fetcher.files["release-manifest.json"] = fetcher.files["release-manifest.json"].replace(
        b"0.5.1", b"0.5.9"
    )
    assert updater(tmp_path, fetcher, [key]).check().status == "refused"


# ---- 4. no downgrade ---------------------------------------------------------------------------


def test_an_older_release_is_refused_as_a_downgrade(tmp_path: Path) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.4.9"), [key], running="0.5.0").check()
    assert check.status == "refused" and "downgrade" in check.message


def test_the_floor_of_a_version_that_ran_here_holds_after_a_rollback(tmp_path: Path) -> None:
    key = Key()
    UpdateState(floor="0.6.0").save(tmp_path / "app" / "state.json")
    check = updater(tmp_path, release(key, "0.5.5"), [key], running="0.5.0").check()
    assert check.status == "refused" and "downgrade" in check.message


def test_the_same_version_is_up_to_date(tmp_path: Path) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.5.0"), [key], running="0.5.0").check()
    assert check.status == "up_to_date"


def test_a_version_that_failed_here_is_not_tried_again(tmp_path: Path) -> None:
    key = Key()
    UpdateState(bad=["0.5.1"]).save(tmp_path / "app" / "state.json")
    assert updater(tmp_path, release(key, "0.5.1"), [key]).check().status == "refused"


# ---- 5. patch installs by itself, minor/major and protocol changes ask -------------------------


def test_a_patch_installs_and_waits_to_be_started(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key])
    check = up.check()
    assert (check.status, check.kind) == ("available", "patch")
    result = up.apply(check)
    assert result.status == "installed", result.message
    assert (tmp_path / "app" / "releases" / "0.5.1" / "nox" / "__init__.py").is_file()
    state = up.state()
    assert (state.pending, state.current, state.attempts) == ("0.5.1", None, 0)
    assert not list((tmp_path / "app" / "releases").glob("*.staging"))
    assert up.restart_needed(running="0.5.0")


@pytest.mark.parametrize(("version", "kind"), [("0.6.0", "minor"), ("1.0.0", "major")])
def test_minor_and_major_updates_wait_for_a_person(tmp_path: Path, version: str, kind: str) -> None:
    key = Key()
    up = updater(tmp_path, release(key, version), [key])
    check = up.check()
    assert (check.status, check.kind) == ("needs_confirmation", kind)
    assert up.apply(check).status == "needs_confirmation"  # nothing happens unasked
    assert not (tmp_path / "app" / "releases").exists()
    assert up.apply(check, confirmed=True).status == "installed"


@pytest.mark.parametrize("protocol", [(2, 2), None])
def test_a_patch_with_another_node_protocol_waits_for_a_person(
    tmp_path: Path, protocol: tuple[int, int] | None
) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.5.1", protocol=protocol), [key]).check()
    assert check.status == "needs_confirmation"


# ---- 6. the bundle must be exactly what the signed manifest lists ------------------------------


def test_a_bundle_that_does_not_match_its_hash_is_refused(tmp_path: Path) -> None:
    key = Key()
    real = make_bundle("0.5.1")
    forged = make_bundle("0.5.1", extra={"nox/evil.py": b"x"})[: len(real)].ljust(len(real), b"0")
    up = updater(tmp_path, release(key, "0.5.1", bundle=real, served=forged), [key])
    result = up.apply(up.check())
    assert result.status == "refused" and "mismatch" in result.message
    assert not (tmp_path / "app" / "releases" / "0.5.1").exists()
    assert up.state().pending is None


def test_a_bundle_larger_than_listed_is_refused(tmp_path: Path) -> None:
    key = Key()
    real = make_bundle("0.5.1")
    up = updater(tmp_path, release(key, "0.5.1", bundle=real, served=real + b"more"), [key])
    assert up.apply(up.check()).status == "refused"


def test_a_release_without_a_node_bundle_says_so(tmp_path: Path) -> None:
    key = Key()
    fetcher = release(key, "0.5.1")
    manifest = build_manifest(tag="v0.5.1", source_commit=COMMIT, assets=[], node_protocol=(1, 1))
    fetcher.files["release-manifest.json"] = manifest
    fetcher.files["release-manifest.json.sig"] = key.sign(manifest)
    up = updater(tmp_path, fetcher, [key])
    result = up.apply(up.check())
    assert result.status == "refused" and "installer" in result.message


# ---- 7./8. contents, libraries, trial start ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "nox/../../outside.py",
        "other/file.py",
        "/abs.py",
        "nox/aux.py",
        "config/x.",
        "nox/a\x1b.py",
    ],
)
def test_a_bundle_with_unexpected_paths_is_refused(tmp_path: Path, name: str) -> None:
    key = Key()
    data = make_bundle("0.5.1", extra={name: b"print('no')"})
    up = updater(tmp_path, release(key, "0.5.1", bundle=data), [key])
    result = up.apply(up.check())
    assert result.status == "refused" and "bad_bundle" in result.message
    assert not (tmp_path / "outside.py").exists()
    assert not (tmp_path / "app" / "releases").exists() or not any(
        (tmp_path / "app" / "releases").iterdir()
    )


def test_a_bundle_built_for_other_library_versions_needs_the_installer(tmp_path: Path) -> None:
    key = Key()
    data = make_bundle("0.5.1", packages={"cryptography": ["99.0.0"]})
    up = updater(
        tmp_path,
        release(key, "0.5.1", bundle=data),
        [key],
        installed=lambda name: "46.0.0" if name == "cryptography" else None,
    )
    result = up.apply(up.check())
    assert result.status == "refused" and "run the installer" in result.message


def test_a_bundle_that_does_not_start_is_never_switched_to(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key], smoke=lambda _app, _py: "")
    result = up.apply(up.check())
    assert result.status == "refused" and "trial_start" in result.message
    assert up.state().pending is None
    assert not (tmp_path / "app" / "releases" / "0.5.1").exists()


# ---- after the restart -------------------------------------------------------------------------


def test_a_working_new_version_becomes_current_and_raises_the_floor(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key])
    up.apply(up.check())
    assert not up.confirm_running("0.5.0")  # not the pending one
    assert up.confirm_running("0.5.1")
    state = up.state()
    assert (state.current, state.pending, state.floor) == ("0.5.1", None, "0.5.1")
    assert not up.restart_needed(running="0.5.1")


def test_the_real_bundle_builds_deterministically_and_installs(tmp_path: Path) -> None:
    """`scripts/build_app_bundle.py` on this checkout, then a confirmed update to it with the
    real trial start (a separate Python process imports the unpacked code)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_app_bundle", REPO / "scripts" / "build_app_bundle.py"
    )
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    version = builder.version_of(REPO)
    first = builder.build(REPO, f"v{version}", tmp_path / "one").read_bytes()
    second = builder.build(REPO, f"v{version}", tmp_path / "two").read_bytes()
    assert first == second
    with pytest.raises(SystemExit):
        builder.build(REPO, "v99.0.0", tmp_path / "three")

    key = Key()
    up = NodeUpdater(
        tmp_path / "app",
        fetcher=release(key, version, bundle=first),
        trusted_keys=lambda: [key.trusted],
        running="0.0.1",
    )
    check = up.check()
    assert check.status == "needs_confirmation"
    result = up.apply(check, confirmed=True)
    assert result.status == "installed", result.message
