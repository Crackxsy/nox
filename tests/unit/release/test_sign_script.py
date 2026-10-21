"""scripts/sign_release.py: self-contained, run as `python -I -S`, key only from stdin, signs only
with a key the release trusts, never leaks it - and its output is what `nox.release` verifies."""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from nox.release import manifest as manifest_module
from nox.release import verify as verify_module
from nox.release.keys import TrustedKey
from nox.release.manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    asset_for_file,
    build_manifest,
    parse_manifest,
)
from nox.release.verify import verify_file, verify_release
from tests.unit.release.conftest import COMMIT, ThrowawayKey

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "sign_release.py"
#: Where this test run's cryptography lives - stands in for the hash-pinned --lib folder of CI.
LIB = Path(sysconfig.get_paths()["platlib"])


def _pem(key: ThrowawayKey) -> str:
    return key.private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _trust_list(tmp_path: Path, *keys: TrustedKey) -> Path:
    entries = "".join(
        f'    TrustedKey(key_id="{k.key_id}", public_key="{k.public_key}", '
        f'not_after="{k.not_after}"),\n'
        for k in keys
    )
    path = tmp_path / "trusted_keys.py"
    path.write_text(
        "from nox.release.keys import TrustedKey\n\n"
        f"TRUSTED_KEYS: tuple[TrustedKey, ...] = (\n{entries})\n",
        "utf-8",
    )
    return path


@pytest.fixture
def assets(tmp_path: Path) -> list[Path]:
    ui = tmp_path / "nox-ui-v0.7.0.tar.gz"
    ui.write_bytes(b"bundle")
    installer = tmp_path / "install-linux.sh"
    installer.write_bytes(b"#!/usr/bin/env bash\n")
    return [ui, installer]


def _run(
    tmp_path: Path,
    assets: list[Path],
    *extra: str,
    stdin: str = "",
    trust: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # -I -S exactly as in the release workflow: no site-packages, no .pth files, no PYTHON*
    # variables, not even the script's folder on the path; cryptography only through --lib.
    command = [
        sys.executable,
        "-I",
        "-S",
        str(SCRIPT),
        "--lib",
        str(LIB),
        "--tag",
        "v0.7.0",
        "--commit",
        COMMIT,
        "--out",
        str(tmp_path / "signed"),
        "--trust-list",
        str(trust or _trust_list(tmp_path)),
        *extra,
        *map(str, assets),
    ]
    # A minimal environment: nothing of this test run's (or CI's) variables leaks in.
    base = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH") if name in os.environ}
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={**base, **(env or {})},
        check=False,
    )


def test_signs_under_python_isolated_without_site_and_the_result_verifies(
    release_key: ThrowawayKey, assets: list[Path], tmp_path: Path
) -> None:
    pem = _pem(release_key)
    output = tmp_path / "github_output"
    result = _run(
        tmp_path,
        assets,
        "--key-stdin",
        stdin=pem,
        trust=_trust_list(tmp_path, release_key.trusted),
        env={"GITHUB_OUTPUT": str(output)},
    )
    assert result.returncode == 0, result.stderr
    out = tmp_path / "signed"
    body = "".join(pem.splitlines()[1:-1])
    for text in (result.stdout, result.stderr, (out / MANIFEST_NAME).read_text()):
        assert body not in text and body[:20] not in text
    assert output.read_text() == "signed=true\n"

    release = verify_release(
        (out / MANIFEST_NAME).read_bytes(),
        (out / SIGNATURE_NAME).read_bytes(),
        trusted_keys=[release_key.trusted],
        expect_tag="v0.7.0",
        expect_commit=COMMIT,
    )
    for path in assets:
        verify_file(release, path)
    assert [key.key_id for key in release.manifest.next_keys] == [release_key.trusted.key_id]


def test_the_layout_is_exactly_what_nox_release_writes(
    release_key: ThrowawayKey, assets: list[Path], tmp_path: Path
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("sign_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    stamp = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
    trusted = [
        {"key_id": k.key_id, "public_key": k.public_key, "not_after": k.not_after}
        for k in (release_key.trusted,)
    ]
    from_script = script.build_manifest(
        tag="v0.7.0-rc.1",
        commit=COMMIT,
        files=assets,
        next_keys=trusted,
        node_protocol=(1, 2),
        min_node_version="0.7.0",
        created=stamp,
    )
    from_nox = build_manifest(
        tag="v0.7.0-rc.1",
        source_commit=COMMIT,
        assets=[asset_for_file(path) for path in assets],
        created=stamp,
        next_keys=[release_key.trusted],
        node_protocol=(1, 2),
        min_node_version="0.7.0",
    )
    assert from_script == from_nox
    parse_manifest(from_script)
    assert script.MANIFEST_SCHEMA == manifest_module.MANIFEST_SCHEMA
    assert script.SIGNATURE_SCHEMA == verify_module.SIGNATURE_SCHEMA
    assert script.PROJECT == manifest_module.PROJECT
    assert script.read_trust_list(REPO / "src" / "nox" / "release" / "trusted_keys.py") == []


def test_the_signer_imports_nothing_but_the_standard_library_and_cryptography() -> None:
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert "nox" not in modules
    third_party = modules - set(sys.stdlib_module_names) - {"__future__"}
    assert third_party == {"cryptography"}


def test_refuses_a_key_this_release_does_not_trust(
    release_key: ThrowawayKey,
    make_key: Callable[..., ThrowawayKey],
    assets: list[Path],
    tmp_path: Path,
) -> None:
    trust = _trust_list(tmp_path, make_key().trusted)
    result = _run(tmp_path, assets, "--key-stdin", stdin=_pem(release_key), trust=trust)
    assert result.returncode == 1
    assert not (tmp_path / "signed" / SIGNATURE_NAME).exists()
    assert not (tmp_path / "signed" / MANIFEST_NAME).exists()
    assert release_key.trusted.key_id in result.stderr and "trusted_keys.py" in result.stderr


def test_refuses_a_trusted_but_expired_key(
    make_key: Callable[..., ThrowawayKey], assets: list[Path], tmp_path: Path
) -> None:
    expired = make_key(not_after="2020-01-01")
    trust = _trust_list(tmp_path, expired.trusted)
    result = _run(tmp_path, assets, "--key-stdin", stdin=_pem(expired), trust=trust)
    assert result.returncode == 1
    assert "expired" in result.stderr
    assert not (tmp_path / "signed" / SIGNATURE_NAME).exists()


def test_a_broken_key_is_refused_without_quoting_it(assets: list[Path], tmp_path: Path) -> None:
    marker = "MC4CAQAwBQYDK2VwBCIEI" + "Zm9vYmFyYmF6"  # key-shaped, invented
    result = _run(tmp_path, assets, "--key-stdin", stdin=f"garbage {marker}")
    assert result.returncode == 1
    assert marker not in result.stdout + result.stderr


def test_a_key_in_the_environment_is_ignored(
    release_key: ThrowawayKey, assets: list[Path], tmp_path: Path
) -> None:
    # Only stdin counts: an environment variable would reach every child process.
    result = _run(
        tmp_path,
        assets,
        trust=_trust_list(tmp_path, release_key.trusted),
        env={"NOX_RELEASE_SIGNING_KEY": _pem(release_key)},
    )
    assert result.returncode == 1
    assert "no signing key" in result.stderr


def test_without_a_key_it_fails_unless_unsigned_is_allowed(
    assets: list[Path], tmp_path: Path
) -> None:
    result = _run(tmp_path, assets, "--key-stdin")
    assert result.returncode == 1
    assert not (tmp_path / "signed" / MANIFEST_NAME).exists()

    output = tmp_path / "github_output"
    env = {"GITHUB_OUTPUT": str(output), "GITHUB_ACTIONS": "true"}
    result = _run(tmp_path, assets, "--key-stdin", "--allow-unsigned", env=env)
    assert result.returncode == 0, result.stderr
    out = tmp_path / "signed"
    assert (out / MANIFEST_NAME).is_file()
    assert not (out / SIGNATURE_NAME).exists()
    assert output.read_text() == "signed=false\n"
    assert "::warning title=Release not signed::" in result.stdout
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert {asset["name"] for asset in manifest["assets"]} == {p.name for p in assets}


def test_a_stale_signature_from_an_earlier_run_never_survives(
    assets: list[Path], tmp_path: Path
) -> None:
    out = tmp_path / "signed"
    out.mkdir()
    (out / SIGNATURE_NAME).write_text("old")
    assert _run(tmp_path, assets, "--allow-unsigned").returncode == 0
    assert not (out / SIGNATURE_NAME).exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--node-protocol", "two"],
        ["--node-protocol", "2-1"],
        ["--min-node-version", "soon"],
    ],
)
def test_bad_metadata_is_refused(assets: list[Path], tmp_path: Path, extra: list[str]) -> None:
    assert _run(tmp_path, assets, "--allow-unsigned", *extra).returncode == 1


def test_a_malformed_trust_list_is_refused(assets: list[Path], tmp_path: Path) -> None:
    trust = tmp_path / "trusted_keys.py"
    trust.write_text('TRUSTED_KEYS = (TrustedKey(key_id="0" * 16),)\n', "utf-8")
    assert _run(tmp_path, assets, "--allow-unsigned", trust=trust).returncode == 1


def test_signing_requirements_match_the_lock_file() -> None:
    import importlib.util

    path = REPO / "scripts" / "update_signing_requirements.py"
    spec = importlib.util.spec_from_file_location("update_signing_requirements", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = module.render((REPO / "uv.lock").read_text("utf-8"))
    actual = (REPO / "scripts" / "signing-requirements.txt").read_text("utf-8")
    assert actual == expected, "run: python scripts/update_signing_requirements.py"
