"""A node updates itself only from a signed release: every check of `nox.node.update` and
`nox.node.bundle`, in the order it runs, and the switch around it. Keys are generated per test and
never stored; nothing here touches the network or an installed copy."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import stat
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nox.node import bundle as bundle_module
from nox.node.bundle import BUNDLE_SCHEMA, BundleError, bundle_name, verify_folder
from nox.node.update import HttpsFetcher, NodeUpdater, UpdateState
from nox.release.keys import TrustedKey, describe_public_key
from nox.release.manifest import Asset, build_manifest

REPO = Path(__file__).resolve().parents[3]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
LOCK = "a" * 64
CREATED = dt.datetime(2026, 10, 21, tzinfo=dt.UTC)


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


def release(  # noqa: PLR0913 - every knob a test turns
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
    fetcher.files["latest/download/release-manifest.json.sig"] = key.sign(b"another manifest")
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
    path = "latest/download/release-manifest.json"
    fetcher.files[path] = fetcher.files[path].replace(b"0.5.1", b"0.5.9")
    assert updater(tmp_path, fetcher, [key]).check().status == "refused"


def test_a_failing_fetch_is_a_refusal_never_an_exception(tmp_path: Path) -> None:
    class Broken(Fetcher):
        def get(self, path: str, limit: int) -> bytes:
            raise RuntimeError("IncompleteRead")

    key = Key()
    check = updater(tmp_path, Broken({}), [key]).check()
    assert check.status == "refused" and "RuntimeError" in check.message


# ---- 4. no downgrade, no replay ----------------------------------------------------------------


def test_an_older_release_is_refused_as_a_downgrade(tmp_path: Path) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.4.9"), [key], running="0.5.0").check()
    assert check.status == "refused" and "downgrade" in check.message


def test_the_floor_of_a_version_that_ran_here_holds_after_a_rollback(tmp_path: Path) -> None:
    key = Key()
    UpdateState(floor="0.6.0").save(tmp_path / "app" / "state.json")
    check = updater(tmp_path, release(key, "0.5.5"), [key], running="0.5.0").check()
    assert check.status == "refused" and "downgrade" in check.message


def test_an_older_manifest_is_refused_as_a_replay(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.0", created=CREATED), [key])
    assert up.check().status == "up_to_date"
    assert up.state().newest_created == "2026-10-21T00:00:00Z"
    older = release(key, "0.5.1", created=CREATED - dt.timedelta(days=3))
    check = updater(tmp_path, older, [key]).check()
    assert check.status == "refused" and "stale" in check.message


def test_a_month_without_a_newer_manifest_is_reported(tmp_path: Path) -> None:
    key = Key()
    seen = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    made = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)
    first = updater(tmp_path, release(key, "0.5.0", created=made), [key], now=seen)
    assert first.check().status == "up_to_date"
    later_clock = seen + dt.timedelta(days=45)
    later = updater(tmp_path, release(key, "0.5.0", created=made), [key], now=later_clock)
    later.check()
    assert later.stale_days() == 45
    assert first.stale_days() is None


def test_a_manifest_from_the_future_is_refused(tmp_path: Path) -> None:
    """A `created` far ahead would block every real release after it: refused, not stored."""
    key = Key()
    now = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)
    ahead = release(key, "0.5.1", created=now + dt.timedelta(days=2))
    up = updater(tmp_path, ahead, [key], now=now)
    check = up.check()
    assert check.status == "refused" and "bad_created" in check.message
    assert up.state().newest_created is None
    almost = release(key, "0.5.1", created=now + dt.timedelta(hours=20))  # a clock a bit behind
    assert updater(tmp_path, almost, [key], now=now).check().status == "available"


def test_the_same_version_is_up_to_date(tmp_path: Path) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.5.0"), [key], running="0.5.0").check()
    assert check.status == "up_to_date"


def test_a_version_that_failed_here_waits_a_week_or_a_deliberate_retry(tmp_path: Path) -> None:
    key = Key()
    now = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)
    failed = (now - dt.timedelta(days=1)).isoformat()
    UpdateState(bad={"0.5.1": failed}).save(tmp_path / "app" / "state.json")
    up = updater(tmp_path, release(key, "0.5.1"), [key], now=now)
    assert up.check().status == "refused"
    retry = up.check(retry_bad=True)
    assert retry.status == "available"
    assert up.apply(retry).status == "installed"
    assert "0.5.1" not in up.state().bad
    later = now + dt.timedelta(days=8)
    UpdateState(bad={"0.5.2": failed}).save(tmp_path / "later" / "app" / "state.json")
    assert (
        updater(tmp_path / "later", release(key, "0.5.2"), [key], now=later).check().status
        == "available"
    )


# ---- 5. policy -----------------------------------------------------------------------------------


def test_a_patch_installs_and_waits_to_be_started(tmp_path: Path) -> None:
    key = Key()
    fetcher = release(key, "0.5.1")
    up = updater(tmp_path, fetcher, [key])
    check = up.check()
    assert (check.status, check.kind) == ("available", "patch")
    result = up.apply(check)
    assert result.status == "installed", result.message
    folder = tmp_path / "app" / "releases" / "0.5.1"
    assert (folder / "nox" / "__init__.py").is_file()
    assert {"bundle.zip", "release-manifest.json", "release-manifest.json.sig"} <= {
        p.name for p in folder.iterdir()
    }
    state = up.state()
    assert (state.pending, state.current, state.failures) == ("0.5.1", None, 0)
    assert not list((tmp_path / "app" / "releases").glob("*.staging"))
    assert up.restart_needed(running="0.5.0")
    # The bundle comes from that tag's own release, not from "latest".
    assert "download/v0.5.1/nox-app-v0.5.1.zip" in fetcher.asked
    # What the launcher checks at every start holds for what was installed.
    assert verify_folder(folder, [key.trusted])["version"] == "0.5.1"


@pytest.mark.parametrize(("version", "kind"), [("0.6.0", "minor"), ("1.0.0", "major")])
def test_minor_and_major_updates_wait_for_a_person(tmp_path: Path, version: str, kind: str) -> None:
    key = Key()
    up = updater(tmp_path, release(key, version), [key])
    check = up.check()
    assert (check.status, check.kind) == ("needs_confirmation", kind)
    assert up.apply(check).status == "needs_confirmation"  # nothing happens unasked
    assert not (tmp_path / "app" / "releases").exists()
    assert up.apply(check, confirmed=True).status == "installed"


def test_a_pre_release_never_installs_by_itself(tmp_path: Path) -> None:
    key = Key()
    check = updater(tmp_path, release(key, "0.5.1-rc.1"), [key]).check()
    assert check.status == "needs_confirmation" and "pre-release" in check.message


# The 0.7.0/0.7.1 case: a 0.7.0 node paired with a core that accepts protocol 1 only; 0.7.1's node
# speaks protocol 2. Installing it by itself would cut the node off from its core.
@pytest.mark.parametrize(
    ("core", "speaks", "status"),
    [
        ((1, 1), 2, "needs_confirmation"),  # the core would refuse the new node
        ((1, 2), 2, "available"),  # the core accepts it: by itself
        ((1, 1), 1, "available"),
        (None, 1, "needs_confirmation"),  # never connected: unknown, so wait
        ((1, 1), None, "needs_confirmation"),  # the manifest does not say
    ],
)
def test_only_a_protocol_the_paired_core_accepts_installs_by_itself(
    tmp_path: Path, core: tuple[int, int] | None, speaks: int | None, status: str
) -> None:
    key = Key()
    data = make_bundle("0.7.1", protocol=speaks or 1)
    fetcher = release(key, "0.7.1", bundle=data, speaks=speaks)
    check = updater(tmp_path, fetcher, [key], running="0.7.0", core=core).check()
    assert check.status == status, check.message


@pytest.mark.parametrize("changed", [True, None])
def test_a_release_that_changes_libraries_needs_the_installer(
    tmp_path: Path, changed: bool | None
) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1", deps_changed=changed), [key])
    check = up.check()
    assert check.status == "needs_installer" and "installer" in check.message
    assert up.apply(check, confirmed=True).status == "needs_installer"


# ---- 6. the bundle must be exactly what the signed manifest lists ------------------------------


def test_a_bundle_that_does_not_match_its_hash_is_refused(tmp_path: Path) -> None:
    key = Key()
    real = make_bundle("0.5.1")
    forged = bytes(reversed(real))
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
    manifest = build_manifest(
        tag="v0.5.1",
        source_commit=COMMIT,
        assets=[],
        node_protocol=(1, 1),
        created=CREATED,
        node_speaks=1,
        deps_changed=False,
    )
    fetcher.files["latest/download/release-manifest.json"] = manifest
    fetcher.files["latest/download/release-manifest.json.sig"] = key.sign(manifest)
    up = updater(tmp_path, fetcher, [key])
    result = up.apply(up.check())
    assert result.status == "refused" and "installer" in result.message


def test_a_download_that_breaks_off_is_a_refusal(tmp_path: Path) -> None:
    class Breaking(Fetcher):
        def download(self, path: str, dest: Path, limit: int) -> None:
            raise RuntimeError("connection reset")

    key = Key()
    up = updater(tmp_path, Breaking(release(key, "0.5.1").files), [key])
    result = up.apply(up.check())
    assert result.status == "refused" and "RuntimeError" in result.message
    assert up.state().pending is None


# ---- 7. contents ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "nox/../../outside.py",
        "other/file.py",
        "/abs.py",
        "nox/a\x1b.py",
        "nox/aux.py",
        "config/x.",
    ],
)
def test_a_bundle_with_unexpected_paths_is_refused(tmp_path: Path, name: str) -> None:
    key = Key()
    data = make_bundle("0.5.1", listed={name: b"print('no')"})
    up = updater(tmp_path, release(key, "0.5.1", bundle=data), [key])
    result = up.apply(up.check())
    assert result.status == "refused"
    assert not (tmp_path / "outside.py").exists()
    assert not (tmp_path / "app" / "releases" / "0.5.1").exists()


def _install(tmp_path: Path, data: bytes) -> str:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1", bundle=data), [key])
    return up.apply(up.check()).message


def test_a_file_the_bundle_does_not_list_is_refused(tmp_path: Path) -> None:
    assert "BUNDLE.json lists" in _install(tmp_path, make_bundle("0.5.1", extra={"nox/x.py": b""}))


def test_a_link_inside_the_bundle_is_refused(tmp_path: Path) -> None:
    out = io.BytesIO()
    data = b"../../etc/passwd"
    files = {"nox/__init__.py": b'__version__ = "0.5.1"\n', "config/defaults.yaml": b"a: 1\n"}
    files["nox/link.py"] = data
    info = {
        "schema": BUNDLE_SCHEMA,
        "version": "0.5.1",
        "node_protocol": 1,
        "lock_sha256": LOCK,
        "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
    }
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("BUNDLE.json", json.dumps(info))
        for name, content in files.items():
            entry = zipfile.ZipInfo(name)
            if name == "nox/link.py":
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, content)
    assert "unexpected entry" in _install(tmp_path, out.getvalue())


def test_too_many_files_or_too_large_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_ENTRIES", 2)
    assert "too many" in _install(tmp_path / "a", make_bundle("0.5.1"))
    monkeypatch.setattr(bundle_module, "MAX_ENTRIES", 5000)
    monkeypatch.setattr(bundle_module, "MAX_UNPACKED_BYTES", 10)
    assert "too large" in _install(tmp_path / "b", make_bundle("0.5.1"))


def test_an_entry_larger_than_its_header_says_is_refused(tmp_path: Path) -> None:
    data = make_bundle("0.5.1", listed={"nox/big.py": b"x" * 4000})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        header = archive.getinfo("nox/big.py")
    patched = bytearray(data)
    local = header.header_offset + 22  # the local header's uncompressed size
    central = data.find(b"PK\x01\x02")
    while data[central + 46 : central + 46 + len(b"nox/big.py")] != b"nox/big.py":
        central = data.find(b"PK\x01\x02", central + 4)
    for where in (local, central + 24):  # ... and the central directory's
        assert int.from_bytes(patched[where : where + 4], "little") == header.file_size
        patched[where : where + 4] = (100).to_bytes(4, "little")
    # The entry now holds more than its headers say: refused, never written past the limit.
    assert "refused" in _install(tmp_path, bytes(patched))
    assert not (tmp_path / "app" / "releases" / "0.5.1").exists()


@pytest.mark.parametrize(
    ("info", "why"),
    [
        ({"schema": "nox-app-bundle/9"}, "unknown bundle format"),
        ({"version": 7}, "no version"),
        ({"files": {"nox/__init__.py": "0" * 64}}, "incomplete"),
        ({"node_protocol": True}, "node protocol"),
        ({"version": "0.5.2"}, "is version"),
        ({"lock_sha256": "b" * 64}, "other library versions"),
        ({"node_protocol": 2}, "another protocol"),
    ],
)
def test_bundle_json_must_describe_this_release(
    tmp_path: Path, info: dict[str, Any], why: str
) -> None:
    assert why in _install(tmp_path, make_bundle("0.5.1", info=info))


def test_a_bundle_that_does_not_start_is_never_switched_to(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key], smoke=lambda _app, _py: "")
    result = up.apply(up.check())
    assert result.status == "refused" and "trial_start" in result.message
    assert up.state().pending is None
    assert not (tmp_path / "app" / "releases" / "0.5.1").exists()


# ---- the folder at every start ----------------------------------------------------------------


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Key]:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key])
    assert up.apply(up.check()).status == "installed"
    return tmp_path / "app" / "releases" / "0.5.1", key


@pytest.mark.parametrize(
    "tamper",
    [
        lambda f: (f / "nox" / "__init__.py").write_text("import os  # planted\n"),
        lambda f: (f / "nox" / "planted.py").write_text("x = 1\n"),
        lambda f: (f / "release-manifest.json.sig").write_bytes(b"{}"),
        lambda f: (f / "bundle.zip").write_bytes(b"PK"),
        lambda f: (f / "BUNDLE.json").write_text("{}"),
        lambda f: (f / "config" / "defaults.yaml").unlink(),
        lambda f: (f / "extra.txt").write_text("x"),
    ],
)
def test_a_changed_release_folder_never_passes(
    installed: tuple[Path, Key], tamper: Callable[[Path], object]
) -> None:
    folder, key = installed
    tamper(folder)
    with pytest.raises((BundleError, ValueError)):
        verify_folder(folder, [key.trusted])


def test_bytecode_caches_are_removed_not_trusted(installed: tuple[Path, Key]) -> None:
    folder, key = installed
    cache = folder / "nox" / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-313.pyc").write_bytes(b"planted")
    verify_folder(folder, [key.trusted])
    assert not cache.exists()


def test_another_key_than_the_bases_does_not_pass(installed: tuple[Path, Key]) -> None:
    folder, _key = installed
    with pytest.raises((BundleError, ValueError)):
        verify_folder(folder, [Key().trusted])


# ---- after the restart -------------------------------------------------------------------------


def test_a_working_new_version_becomes_current_and_raises_the_floor(tmp_path: Path) -> None:
    key = Key()
    up = updater(tmp_path, release(key, "0.5.1"), [key])
    up.apply(up.check())
    assert not up.confirm_running("0.5.0")  # not the pending one
    assert up.on_trial("0.5.1")
    assert up.confirm_running("0.5.1")
    state = up.state()
    assert (state.current, state.pending, state.floor) == ("0.5.1", None, "0.5.1")
    assert not up.restart_needed(running="0.5.1")


def test_pruning_keeps_the_running_and_pending_versions(tmp_path: Path) -> None:
    key = Key()
    releases = tmp_path / "app" / "releases"
    for name in ("0.4.0", "0.5.0", "0.4.9"):
        (releases / name).mkdir(parents=True)
    UpdateState(previous="0.4.9").save(tmp_path / "app" / "state.json")
    up = updater(tmp_path, release(key, "0.5.1"), [key], running="0.5.0")
    assert up.apply(up.check()).status == "installed"
    assert sorted(p.name for p in releases.iterdir()) == ["0.4.9", "0.5.0", "0.5.1"]


# ---- HTTPS ---------------------------------------------------------------------------------------


def test_the_fetcher_only_speaks_https_and_only_release_paths() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpsFetcher("http://github.com/")
    fetcher = HttpsFetcher()
    for path in ("../x", "latest/download/../../x", "download/v1/../x/y", "https://x/y"):
        with pytest.raises(ValueError, match="release file path"):
            fetcher.get(path, 10)


def test_a_redirect_away_from_https_is_refused() -> None:
    from nox.node.update import _HttpsOnly

    request = urllib.request.Request("https://github.com/Crackxsy/nox/releases/latest/download/x")
    with pytest.raises(urllib.error.HTTPError, match="away from HTTPS"):
        _HttpsOnly().redirect_request(request, None, 302, "Found", {}, "http://example.invalid/x")


def test_the_fetcher_caps_what_it_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    fetcher = HttpsFetcher()
    monkeypatch.setattr(fetcher, "_open", lambda _path: Response(b"x" * 100))
    with pytest.raises(Exception, match="larger"):
        fetcher.get("latest/download/release-manifest.json", 10)
    with pytest.raises(Exception, match="larger"):
        fetcher.download("download/v0.5.1/nox-app-v0.5.1.zip", tmp_path / "f", 10)


# ---- the real bundle -----------------------------------------------------------------------------


def test_the_real_bundle_builds_deterministically_and_installs(tmp_path: Path) -> None:
    """`scripts/build_app_bundle.py` on this checkout, then a confirmed update to it with the
    real trial start (a separate, isolated Python process imports the unpacked code)."""
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
        core_range=lambda: (1, 1),
        lock_sha256=lambda: builder.lock_sha256(REPO),
    )
    check = up.check()
    assert check.status == "needs_confirmation"
    result = up.apply(check, confirmed=True)
    assert result.status == "installed", result.message
    verify_folder(tmp_path / "app" / "releases" / version, [key.trusted])


def test_a_release_folder_under_another_name_does_not_pass(
    installed: tuple[Path, Key], tmp_path: Path
) -> None:
    folder, key = installed
    renamed = folder.with_name("0.5.9")
    folder.rename(renamed)
    with pytest.raises(BundleError, match="named after"):
        verify_folder(renamed, [key.trusted])


def test_a_link_inside_a_release_folder_does_not_pass(installed: tuple[Path, Key]) -> None:
    folder, key = installed
    target = folder / "nox" / "__init__.py"
    link = folder / "nox" / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create symbolic links (Windows without developer mode)")
    with pytest.raises(BundleError, match="link"):
        verify_folder(folder, [key.trusted])
    link.unlink()
    original = target.read_bytes()
    target.unlink()
    try:
        target.symlink_to(folder / "config" / "defaults.yaml")
    except OSError:  # pragma: no cover - creatable once, creatable twice
        target.write_bytes(original)
        return
    with pytest.raises(BundleError, match="link"):
        verify_folder(folder, [key.trusted])
