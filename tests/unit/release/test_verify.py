"""Release verification fails closed: every way a manifest can be wrong is a refusal."""

from __future__ import annotations

import base64
import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from nox.release.manifest import Asset, ManifestError, Version, build_manifest, parse_manifest
from nox.release.sign import sign_manifest
from nox.release.verify import (
    ReleaseVerificationError,
    SigningNotConfiguredError,
    verify_file,
    verify_release,
)
from tests.unit.release.conftest import COMMIT, ThrowawayKey

UI = Asset(name="nox-ui-v0.7.0.tar.gz", size=3, sha256="a" * 64)


def _manifest(tag: str = "v0.7.0", **overrides: object) -> bytes:
    arguments: dict[str, object] = {"tag": tag, "source_commit": COMMIT, "assets": [UI]}
    arguments.update(overrides)
    return build_manifest(**arguments)  # type: ignore[arg-type]


def _signature_file(**fields: str) -> bytes:
    document = {
        "schema": "nox-release-signature/1",
        "algorithm": "ed25519",
        "key_id": "0000000000000000",
        "signature": base64.b64encode(bytes(64)).decode(),
    }
    document.update(fields)
    return json.dumps(document).encode()


def _code(excinfo: pytest.ExceptionInfo[ReleaseVerificationError]) -> str:
    return excinfo.value.code


def test_a_signed_manifest_verifies(release_key: ThrowawayKey) -> None:
    manifest = _manifest()
    release = verify_release(
        manifest,
        sign_manifest(manifest, release_key.private),
        trusted_keys=[release_key.trusted],
        expect_tag="v0.7.0",
        expect_commit=COMMIT,
        min_version="0.6.0",
    )
    assert release.key_id == release_key.trusted.key_id
    assert release.manifest.version == Version.parse("0.7.0")
    assert release.manifest.asset(UI.name) == UI


def test_no_trusted_key_is_its_own_outcome_never_verified(release_key: ThrowawayKey) -> None:
    manifest = _manifest()
    with pytest.raises(SigningNotConfiguredError) as excinfo:
        verify_release(manifest, sign_manifest(manifest, release_key.private), trusted_keys=[])
    assert _code(excinfo) == "not_configured"


def test_a_key_that_is_not_trusted_is_refused(
    release_key: ThrowawayKey, make_key: Callable[..., ThrowawayKey]
) -> None:
    manifest = _manifest()
    stranger = make_key()
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            manifest, sign_manifest(manifest, stranger.private), trusted_keys=[release_key.trusted]
        )
    assert _code(excinfo) == "unknown_key"


def test_a_signature_claiming_a_trusted_key_id_but_made_by_another_key_is_refused(
    release_key: ThrowawayKey, make_key: Callable[..., ThrowawayKey]
) -> None:
    manifest = _manifest()
    forged = json.loads(sign_manifest(manifest, make_key().private))
    forged["key_id"] = release_key.trusted.key_id
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(manifest, json.dumps(forged).encode(), trusted_keys=[release_key.trusted])
    assert _code(excinfo) == "bad_signature"


def test_one_changed_byte_breaks_the_signature(release_key: ThrowawayKey) -> None:
    manifest = _manifest()
    signature = sign_manifest(manifest, release_key.private)
    tampered = manifest.replace(b'"size": 3', b'"size": 4')
    assert tampered != manifest
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(tampered, signature, trusted_keys=[release_key.trusted])
    assert _code(excinfo) == "bad_signature"


def test_an_expired_key_is_refused_from_the_day_after(
    make_key: Callable[..., ThrowawayKey],
) -> None:
    key = make_key(not_after="2026-10-21")
    manifest = _manifest()
    signature = sign_manifest(manifest, key.private)
    verify_release(manifest, signature, trusted_keys=[key.trusted], today=dt.date(2026, 10, 21))
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(manifest, signature, trusted_keys=[key.trusted], today=dt.date(2026, 10, 22))
    assert _code(excinfo) == "expired_key"


def test_rotation_old_and_new_key_both_trusted_either_signature_counts(
    make_key: Callable[..., ThrowawayKey],
) -> None:
    old, new = make_key(not_after="2027-06-30"), make_key(not_after="2029-06-30")
    manifest = _manifest()
    for signer in (old, new):
        release = verify_release(
            manifest,
            sign_manifest(manifest, signer.private),
            trusted_keys=[old.trusted, new.trusted],
            today=dt.date(2027, 1, 1),
        )
        assert release.key_id == signer.trusted.key_id
    # After the old key's last day only the new one signs.
    with pytest.raises(ReleaseVerificationError):
        verify_release(
            manifest,
            sign_manifest(manifest, old.private),
            trusted_keys=[old.trusted, new.trusted],
            today=dt.date(2027, 7, 1),
        )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"expect_tag": "v0.7.1"}, "wrong_tag"),
        ({"expect_commit": "f" * 40}, "wrong_commit"),
        ({"min_version": "0.7.1"}, "downgrade"),
        ({"min_version": "v0.8.0"}, "downgrade"),
        ({"min_version": "not-a-version"}, "bad_floor"),
    ],
)
def test_a_valid_signature_on_the_wrong_release_is_refused(
    release_key: ThrowawayKey, kwargs: dict[str, str], code: str
) -> None:
    manifest = _manifest()
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            manifest,
            sign_manifest(manifest, release_key.private),
            trusted_keys=[release_key.trusted],
            **kwargs,  # type: ignore[arg-type]
        )
    assert _code(excinfo) == code


def test_a_deliberate_downgrade_is_accepted_only_when_asked_for(release_key: ThrowawayKey) -> None:
    manifest = _manifest()
    signature = sign_manifest(manifest, release_key.private)
    release = verify_release(
        manifest,
        signature,
        trusted_keys=[release_key.trusted],
        min_version="0.9.0",
        allow_downgrade=True,
    )
    assert str(release.manifest.version) == "0.7.0"
    # The same version again is no downgrade.
    verify_release(manifest, signature, trusted_keys=[release_key.trusted], min_version="0.7.0")


def test_a_pre_release_is_below_its_release(release_key: ThrowawayKey) -> None:
    manifest = _manifest("v0.7.0-rc.1")
    assert parse_manifest(manifest).channel == "prerelease"
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            manifest,
            sign_manifest(manifest, release_key.private),
            trusted_keys=[release_key.trusted],
            min_version="0.7.0",
        )
    assert _code(excinfo) == "downgrade"


def test_a_manifest_for_another_project_is_refused(release_key: ThrowawayKey) -> None:
    document = json.loads(_manifest())
    document["project"] = "someone-else/fork"
    manifest = json.dumps(document).encode()
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            manifest,
            sign_manifest(manifest, release_key.private),
            trusted_keys=[release_key.trusted],
        )
    assert _code(excinfo) == "wrong_project"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="nox-release-manifest/0"),
        lambda d: d.update(tag="v0.7.1"),
        lambda d: d.update(version="0.7"),
        lambda d: d.update(channel="prerelease"),
        lambda d: d.update(source_commit="abc"),
        lambda d: d.update(created="yesterday"),
        lambda d: d["assets"].append(dict(d["assets"][0])),
        lambda d: d["assets"][0].update(name="../../etc/passwd"),
        lambda d: d["assets"][0].update(name="dir/file"),
        lambda d: d["assets"][0].update(size=True),
        lambda d: d["assets"][0].update(size=-1),
        lambda d: d["assets"][0].update(sha256="A" * 64),
        lambda d: d.update(node_protocol={"min": 2, "max": 1}),
        lambda d: d.update(min_node_version="soon"),
        lambda d: d.update(next_keys=[{"key_id": "0" * 16, "public_key": "x", "not_after": "x"}]),
        lambda d: d.pop("assets"),
    ],
)
def test_a_signed_but_malformed_manifest_is_refused(
    release_key: ThrowawayKey, mutate: Callable[[dict[str, object]], object]
) -> None:
    document = json.loads(_manifest())
    mutate(document)
    manifest = json.dumps(document).encode()
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            manifest,
            sign_manifest(manifest, release_key.private),
            trusted_keys=[release_key.trusted],
        )
    assert _code(excinfo) == "bad_manifest"


def test_unknown_fields_are_ignored_so_later_versions_can_add_some(
    release_key: ThrowawayKey,
) -> None:
    document = json.loads(_manifest())
    document["added_later"] = {"anything": 1}
    manifest = json.dumps(document).encode()
    release = verify_release(
        manifest, sign_manifest(manifest, release_key.private), trusted_keys=[release_key.trusted]
    )
    assert release.manifest.extra == {"added_later": {"anything": 1}}


@pytest.mark.parametrize(
    "signature",
    [
        b"",
        b"not json",
        b"[]",
        _signature_file(algorithm="rsa"),
        _signature_file(key_id="../x"),
        _signature_file(signature="%%%"),
        _signature_file(signature=base64.b64encode(b"short").decode()),
        _signature_file(schema="nox-release-signature/2"),
        b" " * 5000,
    ],
)
def test_a_broken_signature_file_is_refused(release_key: ThrowawayKey, signature: bytes) -> None:
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(_manifest(), signature, trusted_keys=[release_key.trusted])
    assert _code(excinfo) == "bad_signature"


def test_files_must_match_the_signed_size_and_hash(
    release_key: ThrowawayKey, tmp_path: Path
) -> None:
    good = tmp_path / "nox-ui-v0.7.0.tar.gz"
    good.write_bytes(b"ui!")
    from nox.release.manifest import asset_for_file

    manifest = _manifest(assets=[asset_for_file(good)])
    release = verify_release(
        manifest, sign_manifest(manifest, release_key.private), trusted_keys=[release_key.trusted]
    )
    verify_file(release, good)

    good.write_bytes(b"ui?")  # same size, other content
    with pytest.raises(ReleaseVerificationError, match="SHA-256"):
        verify_file(release, good)
    good.write_bytes(b"ui!!")
    with pytest.raises(ReleaseVerificationError, match="bytes"):
        verify_file(release, good)
    other = tmp_path / "install-linux.sh"
    other.write_bytes(b"ui!")
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_file(release, other)
    assert _code(excinfo) == "not_listed"


def test_the_manifest_is_deterministic_and_round_trips() -> None:
    stamp = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
    first = _manifest(created=stamp, node_protocol=(1, 2), min_node_version="0.7.0")
    assert first == _manifest(created=stamp, node_protocol=(1, 2), min_node_version="0.7.0")
    parsed = parse_manifest(first)
    assert parsed.node_protocol == (1, 2)
    assert str(parsed.min_node_version) == "0.7.0"
    assert parsed.created == "2026-10-21T12:00:00Z"


@pytest.mark.parametrize("tag", ["0.7.0", "v0.7", "v01.2.3", "v1.2.3-", "v1.2.3+build"])
def test_only_release_tags_get_a_manifest(tag: str) -> None:
    with pytest.raises(ManifestError):
        _manifest(tag)


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("0.6.9", "0.7.0"),
        ("0.7.0-rc.1", "0.7.0"),
        ("0.7.0-alpha", "0.7.0-beta"),
        ("0.7.0-rc.2", "0.7.0-rc.10"),
        ("0.7.0-rc.9", "0.7.0-rc.a"),
        ("0.9.0", "0.10.0"),
    ],
)
def test_versions_are_ordered_by_semantic_versioning(lower: str, higher: str) -> None:
    assert Version.parse(lower) < Version.parse(higher)
    assert Version.parse(higher) > Version.parse(lower)
    assert Version.parse(lower) <= Version.parse(lower)


def test_an_os_image_manifest_never_stands_in_for_the_release(release_key: ThrowawayKey) -> None:
    # Both are signed by the same key (os/README.md); each verifier names the one it expects.
    image = _manifest(kind="os-image")
    signature = sign_manifest(image, release_key.private)
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(image, signature, trusted_keys=[release_key.trusted])
    assert _code(excinfo) == "wrong_kind"
    release = verify_release(
        image, signature, trusted_keys=[release_key.trusted], expect_kind="os-image"
    )
    assert release.manifest.kind == "os-image"
    plain = _manifest()
    with pytest.raises(ReleaseVerificationError) as excinfo:
        verify_release(
            plain,
            sign_manifest(plain, release_key.private),
            trusted_keys=[release_key.trusted],
            expect_kind="os-image",
        )
    assert _code(excinfo) == "wrong_kind"


def test_a_release_manifest_keeps_its_layout_and_unknown_kinds_are_refused() -> None:
    assert b'"kind"' not in _manifest()
    assert json.loads(_manifest(kind="os-image"))["kind"] == "os-image"
    document = json.loads(_manifest())
    document["kind"] = "firmware"
    with pytest.raises(ManifestError):
        parse_manifest(json.dumps(document).encode())
