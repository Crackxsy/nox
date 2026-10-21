"""The bash installer and Nox verify releases the same way: same trusted keys, same formats.

The installer cannot import Nox (on a first install there is none yet), so it carries its own copy
of the key list and a small standard-library checker. These tests keep the copies equal; the CI
job "Installer on Linux" runs the installer's check itself against releases signed here.
"""

from __future__ import annotations

import re
from pathlib import Path

from nox.release import trusted_release_keys
from nox.release.manifest import MANIFEST_SCHEMA, MAX_MANIFEST_BYTES, PROJECT
from nox.release.verify import MAX_SIGNATURE_BYTES, SIGNATURE_SCHEMA

REPO = Path(__file__).resolve().parents[3]
INSTALLER = (REPO / "scripts" / "install-linux.sh").read_text("utf-8")
COMMAND = (REPO / "installer" / "linux" / "nox-command.sh").read_text("utf-8")


def _installer_keys() -> list[str]:
    block = re.search(r"^TRUSTED_RELEASE_KEYS=\(\n(.*?)^\)$", INSTALLER, re.M | re.S)
    assert block is not None, "TRUSTED_RELEASE_KEYS=( ... ) not found in install-linux.sh"
    entries = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r'"([0-9a-f]{16}\|\d{4}-\d{2}-\d{2}\|[A-Za-z0-9+/=]+)"', line)
        assert match, f"malformed TRUSTED_RELEASE_KEYS entry: {line!r}"
        entries.append(match.group(1))
    return entries


def test_the_installer_trusts_exactly_the_keys_nox_trusts() -> None:
    expected = [f"{k.key_id}|{k.not_after}|{k.public_key}" for k in trusted_release_keys()]
    assert _installer_keys() == expected, (
        "scripts/install-linux.sh TRUSTED_RELEASE_KEYS and src/nox/release/trusted_keys.py differ"
    )


def _helper() -> str:
    match = re.search(r"python3 -I - \"\$@\" <<'PY'\n(.*?)^PY$", INSTALLER, re.M | re.S)
    assert match is not None
    return match.group(1)


def test_the_installer_checker_is_valid_python_and_uses_the_same_formats() -> None:
    helper = _helper()
    compile(helper, "install-linux.sh:release_check", "exec")
    assert f'"{SIGNATURE_SCHEMA}"' in helper
    assert f'"{MANIFEST_SCHEMA}"' in helper
    assert f'"{PROJECT}"' in helper
    assert f", {MAX_MANIFEST_BYTES})" in helper
    assert f", {MAX_SIGNATURE_BYTES})" in helper
    # Same SPKI prefix as nox.release.keys.
    assert 'bytes.fromhex("302a300506032b6570032100")' in helper


def test_the_installer_never_uses_a_test_key_list_outside_its_test_action() -> None:
    uses = [m.start() for m in re.finditer(r"NOX_TEST_TRUSTED_KEYS", INSTALLER)]
    action = INSTALLER.index('if [ "$ACTION" = "verify-release" ]; then')
    end = INSTALLER.index("\nfi\n", action)
    assert uses, "the verify-release test hook is gone - update this test"
    assert all(action < position < end for position in uses)
    assert "NOX_TEST_TRUSTED_KEYS" not in COMMAND


def test_nox_update_checks_the_target_with_the_running_version_before_its_installer_runs() -> None:
    update = COMMAND[COMMAND.index("  update)") : COMMAND.index("  doctor)")]
    verify = update.index('verify_target "$target_ref" "$target_commit"')
    run = update.index('sudo env "${version[@]}" bash "$installer_copy"')
    assert verify < run
    assert "|| exit 1" in update[verify:run]
