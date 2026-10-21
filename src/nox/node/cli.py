"""`nox node pair | run | status | log | allow | deny` - the node's own terminal commands.

Everything here runs as the logged-in user and touches only that user's node folder
(`nox.node.home`). `allow` is the one way to widen what Nox may do on this device; nothing the
core sends can do it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from nox.node.agent import NodeAgent
from nox.node.enroll import EnrollError, enroll
from nox.node.gate import NodeGate
from nox.node.home import NodeHome
from nox.node.keystore import NodeKeyStore
from nox.node.manifest import GROUPS, load_manifest, save_manifest
from nox.node.protocol import PairLink, PairLinkError, escape_controls
from nox.node.tools import default_tools
from nox.security.audit import SqliteAuditLog
from nox.security.model import SecretStore
from nox.security.secrets import KeyringSecretStore, keyring_usable

__all__ = ["NodeCliError", "NodeRuntime", "allow", "build", "log_lines", "pair", "run", "status"]

KEYRING_SERVICE = "nox-node"


class NodeCliError(RuntimeError):
    """A message for the person at the terminal."""


@dataclass
class NodeRuntime:
    home: NodeHome
    keystore: NodeKeyStore
    gate: NodeGate
    audit: SqliteAuditLog


def secret_store() -> SecretStore | None:
    """The OS keyring when there is one (Windows, macOS, a desktop Linux); None on a headless
    Linux node, whose key file is then protected by its file mode alone."""
    return KeyringSecretStore(service=KEYRING_SERVICE) if keyring_usable() else None


def open_audit(home: NodeHome) -> SqliteAuditLog:
    home.ensure()
    return SqliteAuditLog(sqlite3.connect(home.audit_file, check_same_thread=False))


def build(
    home: NodeHome, *, secrets: SecretStore | None = None, notifier: object = None
) -> NodeRuntime:
    home.ensure()
    audit = open_audit(home)
    manifest = load_manifest(home.manifest_file)
    gate = NodeGate(default_tools(notifier), manifest, audit)  # type: ignore[arg-type]
    keystore = NodeKeyStore(home.key_file, secrets)
    return NodeRuntime(home, keystore, gate, audit)


_REASONS = {
    "fingerprint_mismatch": "The device answering is not the Nox from the link (its "
    "fingerprint differs). Nothing was sent. Pair only in your own network.",
    "unreachable": "Nox is not reachable at the address in the link.",
    "already_paired": "This device is already paired. Remove it on the core first (Devices page).",
    "used": "The code was already used. Make a new one.",
    "expired": "The code has expired. Make a new one.",
    "unknown_code": "Nox does not know this code.",
    "max_devices": "Nox has as many nodes as it accepts.",
    "rate_limited": "Too many attempts. Wait a minute.",
    "name_taken": "Name ähnelt einem vorhandenen Gerät - the name looks like a device Nox already "
    "has. Choose another one with --name.",
    "invalid_characters": "Bitte nur Buchstaben, Ziffern, Leerzeichen und - _ . "
    "(letters A-Z, ä ö ü ß, digits, space, - _ .). Choose another name with --name.",
    "not_nox": "The device at that address does not answer like Nox.",
    "key_not_protected": "The key file could not be made readable by this user only.",
}


def pair(text: str, *, home: NodeHome, name: str = "", secrets: SecretStore | None = None) -> str:
    try:
        link = PairLink.parse(text)
    except PairLinkError as exc:
        raise NodeCliError(f"Not a Nox node link: {exc}") from exc
    keystore = NodeKeyStore(home.key_file, secrets)
    try:
        identity = asyncio.run(enroll(link, home, keystore, name=name))
    except EnrollError as exc:
        raise NodeCliError(_REASONS.get(exc.reason, f"Pairing failed: {exc.reason}")) from exc
    load_manifest(home.manifest_file)  # writes the defaults when there are none yet
    return f"Paired as {identity.name!r}. Start it with: nox node run"


def run(runtime: NodeRuntime, *, install_signals: bool = True) -> str:
    async def main() -> str:
        stop = asyncio.Event()
        if install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop.set)
        agent = NodeAgent(runtime.home, runtime.keystore, runtime.gate, runtime.audit)
        return await agent.run(stop)

    outcome = asyncio.run(main())
    return {
        "not_paired": "This device is not paired yet: nox node pair <link>",
        "revoked": "Removed from Nox (Von Nox entfernt). Key and certificate are deleted.",
        "pin_mismatch": "The stored certificate authority is not the pinned one; pair again.",
        "stopped": "Stopped.",
    }.get(outcome, outcome)


def status(home: NodeHome) -> list[str]:
    identity = home.load_identity()
    if identity is None:
        last = home.read_status().get("state")
        return ["Removed from Nox." if last == "removed" else "Not paired."]
    manifest = load_manifest(home.manifest_file)
    state = home.read_status()
    allowed = [GROUPS[g].label_en for g in GROUPS if manifest.groups.get(g)]
    x = escape_controls
    return [
        f"Name:      {x(identity.name)}",
        f"Core:      {x(identity.core_host)}:{identity.node_port}",
        f"State:     {x(str(state.get('state', 'not running')))} ({x(str(state.get('at', '-')))})",
        f"Nox may:   {', '.join(allowed) or 'nothing'}",
        f"Max risk:  {manifest.max_risk}",
    ]


def log_lines(home: NodeHome, *, limit: int = 50) -> list[str]:
    audit = open_audit(home)
    entries = audit.entries(since_seq=max(0, audit.count() - limit), limit=limit)
    # Every field is escaped on the way out too: an entry written before this check existed,
    # or by anything else, still cannot drive the terminal or fake a line.
    x = escape_controls
    lines = [
        f"{e.seq:>6}  {x(e.decision):<5}  {x(e.actor):<6} {x(e.tool):<14} {x(e.action):<12} "
        f"{x(e.target)} {x(e.result)}".rstrip()
        for e in entries
    ]
    lines.append("Chain intact." if audit.verify_chain() else "CHAIN BROKEN - the log was altered.")
    return lines


def allow(home: NodeHome, group: str, enabled: bool, *, say: Callable[[str], None]) -> None:
    if group not in GROUPS:
        raise NodeCliError(f"Unknown group {group!r}. Known: {', '.join(GROUPS)}")
    manifest = load_manifest(home.manifest_file)
    manifest.groups[group] = enabled
    save_manifest(home.manifest_file, manifest)
    audit = open_audit(home)
    audit.append(
        actor="local",
        tool="node",
        action="node.allow" if enabled else "node.deny",
        target=group,
        decision="allow",
        result="ok",
    )
    say(f"{'Allowed' if enabled else 'Denied'}: {GROUPS[group].label_en}. Restart `nox node run`.")
