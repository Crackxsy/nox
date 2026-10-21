"""scripts/sign_release.py: signs only with a trusted key, never leaks it, and without a key either
fails or - when told to - writes an unsigned manifest and says so loudly."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization

import nox.release.trusted_keys as trusted_module
from nox.release.manifest import MANIFEST_NAME, SIGNATURE_NAME
from nox.release.verify import verify_file, verify_release
from tests.unit.release.conftest import COMMIT, ThrowawayKey

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "sign_release.py"
VARIABLE = "NOX_RELEASE_SIGNING_KEY"


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sign_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pem(key: ThrowawayKey) -> str:
    return key.private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def assets(tmp_path: Path) -> list[Path]:
    ui = tmp_path / "nox-ui-v0.7.0.tar.gz"
    ui.write_bytes(b"bundle")
    installer = tmp_path / "install-linux.sh"
    installer.write_bytes(b"#!/usr/bin/env bash\n")
    return [ui, installer]


def _args(out: Path, assets: list[Path], *extra: str) -> list[str]:
    return ["--tag", "v0.7.0", "--commit", COMMIT, "--out", str(out), *extra, *map(str, assets)]


def test_signs_with_a_trusted_key_from_the_environment_and_never_prints_it(
    script: ModuleType,
    release_key: ThrowawayKey,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (release_key.trusted,))
    pem = _pem(release_key)
    monkeypatch.setenv(VARIABLE, pem)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "signed"

    assert script.main(_args(out, assets)) == 0

    import os

    assert VARIABLE not in os.environ, "the key must not stay in the environment"
    captured = capsys.readouterr()
    body = "".join(pem.splitlines()[1:-1])
    for text in (captured.out, captured.err, (out / MANIFEST_NAME).read_text()):
        assert body not in text and body[:20] not in text
    assert output.read_text() == "signed=true\n"

    manifest = (out / MANIFEST_NAME).read_bytes()
    release = verify_release(
        manifest,
        (out / SIGNATURE_NAME).read_bytes(),
        trusted_keys=[release_key.trusted],
        expect_tag="v0.7.0",
        expect_commit=COMMIT,
    )
    for path in assets:
        verify_file(release, path)
    # The release announces the keys it will trust for the next update.
    assert [key.key_id for key in release.manifest.next_keys] == [release_key.trusted.key_id]


def test_reads_the_key_from_stdin(
    script: ModuleType,
    release_key: ThrowawayKey,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (release_key.trusted,))
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_pem(release_key)))
    out = tmp_path / "signed"
    assert script.main(_args(out, assets, "--key-stdin")) == 0
    assert (out / SIGNATURE_NAME).is_file()


def test_refuses_a_key_this_release_does_not_trust(
    script: ModuleType,
    release_key: ThrowawayKey,
    make_key: Callable[..., ThrowawayKey],
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (make_key().trusted,))
    monkeypatch.setenv(VARIABLE, _pem(release_key))
    out = tmp_path / "signed"
    assert script.main(_args(out, assets)) == 1
    assert not (out / SIGNATURE_NAME).exists()
    assert not (out / MANIFEST_NAME).exists()
    err = capsys.readouterr().err
    assert release_key.trusted.key_id in err and "trusted_keys.py" in err


def test_refuses_a_trusted_but_expired_key(
    script: ModuleType,
    make_key: Callable[..., ThrowawayKey],
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = make_key(not_after="2020-01-01")
    monkeypatch.setattr(trusted_module, "TRUSTED_KEYS", (expired.trusted,))
    monkeypatch.setenv(VARIABLE, _pem(expired))
    assert script.main(_args(tmp_path / "signed", assets)) == 1
    assert not (tmp_path / "signed" / SIGNATURE_NAME).exists()


def test_a_broken_key_is_refused_without_quoting_it(
    script: ModuleType,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "MC4CAQAwBQYDK2VwBCIEI" + "Zm9vYmFyYmF6"  # key-shaped, invented
    monkeypatch.setenv(VARIABLE, f"garbage {marker}")
    assert script.main(_args(tmp_path / "signed", assets)) == 1
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err


def test_without_a_key_it_fails_unless_unsigned_is_allowed(
    script: ModuleType,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    out = tmp_path / "signed"
    assert script.main(_args(out, assets)) == 1
    assert not (out / MANIFEST_NAME).exists()

    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert script.main(_args(out, assets, "--allow-unsigned")) == 0
    assert (out / MANIFEST_NAME).is_file()
    assert not (out / SIGNATURE_NAME).exists()
    assert output.read_text() == "signed=false\n"
    assert "::warning title=Release not signed::" in capsys.readouterr().out
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert {asset["name"] for asset in manifest["assets"]} == {p.name for p in assets}


def test_a_stale_signature_from_an_earlier_run_never_survives(
    script: ModuleType,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "signed"
    out.mkdir()
    (out / SIGNATURE_NAME).write_text("old")
    monkeypatch.delenv(VARIABLE, raising=False)
    assert script.main(_args(out, assets, "--allow-unsigned")) == 0
    assert not (out / SIGNATURE_NAME).exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--node-protocol", "two"],
    ],
)
def test_bad_metadata_is_refused(
    script: ModuleType,
    assets: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    assert script.main(_args(tmp_path / "o", assets, "--allow-unsigned", *extra)) == 1


def test_a_short_commit_or_a_non_release_tag_is_refused(
    script: ModuleType, assets: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    base = ["--out", str(tmp_path / "o"), "--allow-unsigned", *map(str, assets)]
    assert script.main(["--tag", "v0.7.0", "--commit", "abc123", *base]) == 1
    assert script.main(["--tag", "develop", "--commit", COMMIT, *base]) == 1
