"""`nox release verify|keys|describe-key`: exit codes scripts can rely on, fail-closed output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

import nox.release.trusted_keys as trusted_module
from nox.cli import app
from nox.release.cli import EXIT_NOT_CONFIGURED, EXIT_REFUSED
from nox.release.manifest import asset_for_file, build_manifest
from nox.release.sign import sign_manifest
from tests.unit.release.conftest import COMMIT, ThrowawayKey

runner = CliRunner()


@pytest.fixture
def release_dir(tmp_path: Path, release_key: ThrowawayKey) -> Path:
    ui = tmp_path / "nox-ui-v0.7.0.tar.gz"
    ui.write_bytes(b"bundle")
    manifest = build_manifest(tag="v0.7.0", source_commit=COMMIT, assets=[asset_for_file(ui)])
    (tmp_path / "release-manifest.json").write_bytes(manifest)
    (tmp_path / "release-manifest.json.sig").write_bytes(
        sign_manifest(manifest, release_key.private)
    )
    return tmp_path


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch, release_key: ThrowawayKey) -> None:
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (release_key.trusted,))


def _verify(release_dir: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(
        app, ["release", "verify", str(release_dir / "release-manifest.json"), *extra]
    )
    return result.exit_code, result.output


@pytest.mark.usefixtures("trusted")
def test_verify_accepts_a_signed_release_and_its_files(release_dir: Path) -> None:
    code, output = _verify(
        release_dir,
        "--file",
        str(release_dir / "nox-ui-v0.7.0.tar.gz"),
        "--expect-tag",
        "v0.7.0",
        "--expect-commit",
        COMMIT,
        "--min-version",
        "v0.6.0",
    )
    assert code == 0, output
    assert "Verified: Nox v0.7.0" in output
    assert "ok  nox-ui-v0.7.0.tar.gz" in output


@pytest.mark.usefixtures("trusted")
def test_verify_json(release_dir: Path) -> None:
    code, output = _verify(release_dir, "--json")
    assert code == 0
    result = json.loads(output)
    assert result["verified"] is True and result["source_commit"] == COMMIT


@pytest.mark.usefixtures("trusted")
@pytest.mark.parametrize(
    "extra",
    [
        ["--expect-tag", "v0.7.1"],
        ["--expect-commit", "f" * 40],
        ["--min-version", "0.8.0"],
        ["--file", "missing-file.tar.gz"],
    ],
)
def test_verify_refuses_with_exit_1(release_dir: Path, extra: list[str]) -> None:
    code, output = _verify(release_dir, *extra)
    assert code == EXIT_REFUSED
    assert "Refused" in output
    assert "Verified" not in output


@pytest.mark.usefixtures("trusted")
def test_a_changed_file_is_refused(release_dir: Path) -> None:
    (release_dir / "nox-ui-v0.7.0.tar.gz").write_bytes(b"bundlE")
    code, _ = _verify(release_dir, "--file", str(release_dir / "nox-ui-v0.7.0.tar.gz"))
    assert code == EXIT_REFUSED


@pytest.mark.usefixtures("trusted")
def test_a_missing_signature_is_refused(release_dir: Path) -> None:
    (release_dir / "release-manifest.json.sig").unlink()
    code, output = _verify(release_dir)
    assert code == EXIT_REFUSED
    assert "cannot read the signature" in output


def test_without_trusted_keys_verify_says_not_configured(
    release_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", ())
    code, output = _verify(release_dir)
    assert code == EXIT_NOT_CONFIGURED
    assert "not configured" in output
    assert "Verified" not in output


def test_keys_lists_what_is_trusted(
    monkeypatch: pytest.MonkeyPatch, release_key: ThrowawayKey
) -> None:
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", ())
    assert runner.invoke(app, ["release", "keys"]).exit_code == EXIT_NOT_CONFIGURED
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (release_key.trusted,))
    result = runner.invoke(app, ["release", "keys"])
    assert result.exit_code == 0
    assert release_key.trusted.key_id in result.output and "valid" in result.output


def test_describe_key_prints_both_entries_and_refuses_a_private_key(
    tmp_path: Path, release_key: ThrowawayKey
) -> None:
    public = tmp_path / "release.pub.pem"
    public.write_bytes(
        release_key.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    result = runner.invoke(
        app, ["release", "describe-key", str(public), "--not-after", "2028-10-31"]
    )
    assert result.exit_code == 0, result.output
    key = release_key.trusted
    assert f'key_id="{key.key_id}"' in result.output
    assert f'"{key.key_id}|2028-10-31|{key.public_key}"' in result.output

    private = tmp_path / "release.key.pem"
    private.write_bytes(
        release_key.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    result = runner.invoke(
        app, ["release", "describe-key", str(private), "--not-after", "2028-10-31"]
    )
    assert result.exit_code == EXIT_REFUSED
    assert "private key" in result.output
