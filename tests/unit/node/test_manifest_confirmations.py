"""What this device allows is decided by a person at this device: `nox node allow` needs an
interactive terminal and a yes, an edit of `node.yaml` widens nothing, and a forged,
replayed or restored confirmation is rejected by the audit chain."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.unit.node.support import node_home


def _yes(home: Any, group: str) -> None:
    from nox.node.device.cli import allow as cli_allow

    cli_allow.allow(
        home, group, True, say=lambda _t: None, interactive=lambda: True, confirm=lambda _p: True
    )


def test_allowing_needs_an_interactive_terminal_and_a_yes(tmp_path: Path) -> None:
    from nox.node.device.cli import allow as cli_allow
    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.wire.manifest import load_manifest

    home = node_home(tmp_path)
    quiet = dict(say=lambda _t: None)
    with pytest.raises(cli_runtime.NodeCliError, match="interactive"):
        cli_allow.allow(
            home, "shell", True, interactive=lambda: False, confirm=lambda _p: True, **quiet
        )
    with pytest.raises(cli_runtime.NodeCliError, match="Not allowed"):
        cli_allow.allow(
            home, "shell", True, interactive=lambda: True, confirm=lambda _p: False, **quiet
        )
    assert not home.manifest_file.exists() or not load_manifest(home.manifest_file).groups["shell"]
    # Taking something away needs no person: narrowing is always safe.
    _yes(home, "notify")
    cli_allow.allow(home, "notify", False, interactive=lambda: False, **quiet)
    assert load_manifest(home.manifest_file).groups["notify"] is False


def _effective(home: Any) -> Any:
    from nox.node.device.cli import runtime as cli_runtime

    return cli_runtime.build(home, notifier=None).gate.manifest


def test_an_edit_of_node_yaml_widens_nothing(tmp_path: Path) -> None:
    from nox.node.wire.manifest import load_manifest, save_manifest

    home = node_home(tmp_path)
    _yes(home, "notify")
    manifest = load_manifest(home.manifest_file)
    manifest.groups["shell"] = True  # written by some process of this user, not confirmed
    manifest.groups["notify"] = False  # ... and a narrowing, which applies
    save_manifest(home.manifest_file, manifest)
    effective = _effective(home)
    assert effective.groups["shell"] is False and effective.groups["notify"] is False


def test_a_forged_confirmation_is_not_trusted(tmp_path: Path) -> None:
    import json as _json

    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.device.confirmed import CONFIRM_ACTION
    from nox.node.wire.manifest import GROUPS, NodeManifest, save_manifest

    home = node_home(tmp_path)
    _yes(home, "notify")
    everything = NodeManifest(groups={g: True for g in GROUPS})
    save_manifest(home.manifest_file, everything)
    audit = cli_runtime.open_audit(home)
    for mac in ("", "0" * 64):  # no MAC, a made-up MAC
        audit.append(
            actor="local",
            tool="node",
            action=CONFIRM_ACTION,
            target="",
            decision="allow",
            result="ok",
            details={"manifest": _json.dumps(everything.model_dump()), "mac": mac},
        )
    effective = _effective(home)
    assert effective.enabled() == {"system"} and effective.max_risk == "read"


def test_a_broken_audit_chain_trusts_no_confirmation(tmp_path: Path) -> None:
    from nox.node.device.cli import runtime as cli_runtime

    home = node_home(tmp_path)
    _yes(home, "notify")
    conn = sqlite3.connect(home.audit_file)
    # Someone with the file: the append-only triggers go first, then a row is changed.
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_log'"
    ).fetchall()
    for (name,) in triggers:
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute("UPDATE audit_log SET result = 'tampered' WHERE seq = 1")
    conn.commit()
    conn.close()
    assert cli_runtime.open_audit(home).verify_chain() is False
    assert _effective(home).enabled() == {"system"}


def test_without_its_key_no_confirmation_counts(tmp_path: Path) -> None:
    home = node_home(tmp_path)
    _yes(home, "notify")
    (home.path / "manifest.key").unlink()
    assert _effective(home).enabled() == {"system"}


async def test_deny_takes_effect_in_the_running_node(tmp_path: Path) -> None:
    from nox.node.device.agent.node_agent import NodeAgent
    from nox.node.device.cli import allow as cli_allow
    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.device.keystore import NodeKeyStore

    home = node_home(tmp_path)
    _yes(home, "notify")
    runtime = cli_runtime.build(home, notifier=None)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        runtime.gate,
        runtime.audit,
        manifest_source=cli_runtime.manifest_source(runtime),
    )
    agent.reload_manifest()
    assert "notify.show" in [t.name for t in runtime.gate.offered()]
    import os

    cli_allow.allow(home, "notify", False, say=lambda _t: None)
    stat = home.manifest_file.stat()
    os.utime(home.manifest_file, (stat.st_atime, stat.st_mtime + 5))
    assert agent.reload_manifest() is True
    assert "notify.show" not in [t.name for t in runtime.gate.offered()]


# ---- cheap LOW points --------------------------------------------------------------------------


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows ACLs")
def test_the_node_folder_is_owner_only_on_windows(tmp_path: Path) -> None:
    import subprocess

    home = node_home(tmp_path)
    acl = subprocess.run(["icacls", str(home.path)], capture_output=True, text=True, check=False)
    text = acl.stdout
    assert "(OI)(CI)" in text
    assert "(I)" not in text  # nothing inherited from the parent folder any more
    for broad in ("Everyone", "Jeder", "BUILTIN\\Users", "Authenticated Users", "Authentifizierte"):
        assert broad not in text, text


def test_an_old_wider_confirmation_appended_again_is_rejected(tmp_path: Path) -> None:
    """Review follow-up: a once valid confirmation appended again (old place, old counter)."""
    from nox.node.device.cli import allow as cli_allow
    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.device.confirmed import CONFIRM_ACTION
    from nox.node.wire.manifest import load_manifest, save_manifest

    home = node_home(tmp_path)
    _yes(home, "shell")  # a wide confirmation ...
    audit = cli_runtime.open_audit(home)
    old = [e for e in audit.entries(limit=500) if e.action == CONFIRM_ACTION][-1]
    old_details = audit.details(old.seq)
    cli_allow.allow(home, "shell", False, say=lambda _t: None)  # ... later narrowed
    assert _effective(home).groups["shell"] is False
    # Someone appends the old wide confirmation again and widens the file.
    audit.append(
        actor="local",
        tool="node",
        action=CONFIRM_ACTION,
        target="",
        decision="allow",
        result="ok",
        details=old_details,
    )
    manifest = load_manifest(home.manifest_file)
    manifest.groups["shell"] = True
    save_manifest(home.manifest_file, manifest)
    assert _effective(home).enabled() == {"system"}  # nothing trusted: most restrictive


def test_pairing_never_adopts_a_widened_file(tmp_path: Path) -> None:
    from nox.node.device.cli import pairing as cli_pairing
    from nox.node.wire.manifest import GROUPS, NodeManifest, load_manifest, save_manifest

    home = node_home(tmp_path, paired=False)
    save_manifest(
        home.manifest_file,
        NodeManifest(groups={g: True for g in GROUPS}, trust_core_for_high=True),
    )
    start = cli_pairing.confirm_defaults(home, None)
    assert start.enabled() == {"system", "notify", "media"} and not start.trust_core_for_high
    assert load_manifest(home.manifest_file).enabled() == {"system", "notify", "media"}
    assert _effective(home).enabled() == {"system", "notify", "media"}


def test_a_confirmation_with_the_right_counter_but_a_made_up_mac_is_rejected(
    tmp_path: Path,
) -> None:
    import json as _json

    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.device.confirmed import CONFIRM_ACTION, manifest_key
    from nox.node.wire.manifest import GROUPS, NodeManifest, save_manifest

    home = node_home(tmp_path)
    loaded = manifest_key(home.path, None).load()
    assert loaded is not None
    everything = NodeManifest(groups={g: True for g in GROUPS})
    save_manifest(home.manifest_file, everything)
    cli_runtime.open_audit(home).append(
        actor="local",
        tool="node",
        action=CONFIRM_ACTION,
        target="",
        decision="allow",
        result="ok",
        details={
            "manifest": _json.dumps(everything.model_dump(), sort_keys=True),
            "counter": str(loaded[1]),  # the current counter - only the MAC is wrong
            "mac": "1" * 64,
        },
    )
    assert _effective(home).enabled() == {"system"}


def test_an_older_copy_of_the_audit_trail_restored_is_rejected(tmp_path: Path) -> None:
    """The whole file swapped for an older copy: its last confirmation is genuine and in place,
    but its counter is behind the one kept with the key."""
    import shutil

    from nox.node.device.cli import allow as cli_allow
    from nox.node.device.cli import runtime as cli_runtime
    from nox.node.wire.manifest import load_manifest, save_manifest

    home = node_home(tmp_path)
    _yes(home, "shell")
    shutil.copy(home.audit_file, tmp_path / "audit-old.db")
    cli_allow.allow(home, "shell", False, say=lambda _t: None)
    shutil.copy(tmp_path / "audit-old.db", home.audit_file)
    manifest = load_manifest(home.manifest_file)
    manifest.groups["shell"] = True
    save_manifest(home.manifest_file, manifest)
    assert cli_runtime.open_audit(home).verify_chain()  # a consistent chain ...
    assert _effective(home).enabled() == {"system"}  # ... but not the current confirmation
