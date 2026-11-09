"""Which capability manifest the person at this device actually confirmed.

`node.yaml` is an ordinary file of the logged-in user - so is everything a process running as
that user can write, a node tool included. A widening therefore only counts when it was confirmed
in a terminal (`nox node allow`, interactive) and recorded in the node's audit trail as a
`node.manifest` entry **with an HMAC** under a key created at pairing: in the OS keyring where
there is one, otherwise `manifest.key` (0600) inside the node folder, which the node's own tools
may never touch (`NodeGate`, `touches`). An entry without a valid MAC, a broken audit chain or a
missing key is never trusted: the node then falls back to the most restrictive manifest (system
values only) and says that the manifest needs to be confirmed again here.

What the file says beyond the last confirmed manifest is not applied: the effective manifest is
the intersection (groups, limits and the folders `file.*` may open), every unconfirmed widening
is audited, and the person is told how to confirm it. Narrowing (a group switched off in the
file) always applies at once.

Residual risk (SECURITY.md): malware running as this user with full user rights can read the
keyring entry or the key file like any other secret of that user.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
from collections.abc import Callable
from pathlib import Path

from nox.ipc.tokens import write_secret_file
from nox.node.manifest import GROUPS, NodeManifest
from nox.security.audit import SqliteAuditLog
from nox.security.model import SecretStore

__all__ = [
    "CONFIRM_ACTION",
    "ConfirmationKey",
    "KEY_SECRET",
    "MOST_RESTRICTIVE",
    "effective_manifest",
    "last_confirmed",
    "manifest_key",
    "record_confirmed",
]

CONFIRM_ACTION = "node.manifest"
KEY_SECRET = "nox/node/manifest_key"  # noqa: S105 - the secret's name, not a secret
KEY_FILE = "manifest.key"
_RISKS = ("read", "low", "medium", "high")
#: `prev_hash` of the very first audit entry (`nox.security.audit`).
_GENESIS = "0" * 64
#: What a node does while nothing trustworthy says otherwise: read its own system values.
MOST_RESTRICTIVE = NodeManifest(
    groups={g: g == "system" for g in GROUPS}, max_risk="read", trust_core_for_high=False
)


class ConfirmationKey:
    """The confirmation key and its counter, stored together: in the keyring as `<hex>:<n>`,
    else in `manifest.key` (0600). Each confirmation raises the counter; only an entry carrying
    the current one is valid, so an old (wider) confirmation re-appended later is worthless."""

    def __init__(self, home_path: Path, store: SecretStore | None) -> None:
        self._path = home_path / KEY_FILE
        self._store = store

    def _read(self) -> str | None:
        if self._store is not None:
            return self._store.get(KEY_SECRET)
        return self._path.read_text(encoding="ascii").strip() if self._path.is_file() else None

    def _write(self, value: str) -> bool:
        if self._store is not None:
            self._store.set(KEY_SECRET, value)
            return True
        if not write_secret_file(self._path, value):
            self._path.unlink(missing_ok=True)
            return False
        return True

    def load(self) -> tuple[bytes, int] | None:
        try:
            raw = self._read()
            if not raw:
                return None
            key_hex, _, counter = raw.partition(":")
            return bytes.fromhex(key_hex), int(counter or "0")
        except Exception:  # noqa: BLE001 - unreadable means: no key, fail closed
            return None

    def advance(self, *, create: bool) -> tuple[bytes, int] | None:
        """The key with its counter raised by one (made first with `create`); None if it cannot
        be stored - then nothing is confirmed."""
        current = self.load()
        if current is None:
            if not create:
                return None
            current = (bytes.fromhex(_secrets.token_hex(32)), 0)
        key, counter = current[0], current[1] + 1
        try:
            ok = self._write(f"{key.hex()}:{counter}")
        except Exception:  # noqa: BLE001
            return None
        return (key, counter) if ok else None


def manifest_key(home_path: Path, store: SecretStore | None) -> ConfirmationKey:
    return ConfirmationKey(home_path, store)


def _mac(key: bytes, seq: int, prev_hash: str, counter: int, body: str) -> str:
    message = f"{CONFIRM_ACTION}\n{seq}\n{prev_hash}\n{counter}\n{body}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def record_confirmed(
    audit: SqliteAuditLog,
    manifest: NodeManifest,
    key: ConfirmationKey,
    *,
    by: str = "local",
    create: bool = False,
) -> bool:
    """Append a confirmation bound to its place in the chain and to a fresh counter value."""
    advanced = key.advance(create=create)
    if advanced is None:
        return False
    secret, counter = advanced
    body = json.dumps(manifest.model_dump(), sort_keys=True)
    with audit._lock:  # the entry's place in the chain must not move between read and append
        last = audit.entries(since_seq=max(audit.count() - 1, 0), limit=1)
        seq = (last[-1].seq + 1) if last else 1
        prev_hash = last[-1].hash if last else _GENESIS
        audit.append(
            actor=by,
            tool="node",
            action=CONFIRM_ACTION,
            target="",
            decision="allow",
            result="ok",
            details={
                "manifest": body,
                "counter": str(counter),
                "mac": _mac(secret, seq, prev_hash, counter, body),
            },
        )
    return True


def last_confirmed(audit: SqliteAuditLog, key: ConfirmationKey | None) -> NodeManifest | None:
    """The last confirmed manifest, or None when none can be trusted: no key, a broken chain, a
    missing confirmation, one whose MAC does not match its place in the chain, or one that does
    not carry the current counter (an old confirmation appended again)."""
    loaded = key.load() if key is not None else None
    if loaded is None or not audit.verify_chain():
        return None
    secret, counter = loaded
    entries = audit.entries(since_seq=0, limit=max(audit.count(), 1))
    for entry in reversed(entries):
        if entry.action != CONFIRM_ACTION:
            continue
        details = audit.details(entry.seq)
        body, mac = details.get("manifest", ""), details.get("mac", "")
        if details.get("counter") != str(counter) or not body:
            return None
        expected = _mac(secret, entry.seq, entry.prev_hash, counter, body)
        if not hmac.compare_digest(mac, expected):
            return None  # the newest confirmation is forged: trust none of them
        try:
            return NodeManifest.model_validate_json(body)
        except ValueError:
            return None
    return None


def effective_manifest(
    current: NodeManifest,
    audit: SqliteAuditLog,
    key: ConfirmationKey | None,
    *,
    say: Callable[[str], None] | None = None,
) -> NodeManifest:
    """What the file says, but never more than was confirmed - and the most restrictive manifest
    when no confirmation can be trusted."""
    confirmed = last_confirmed(audit, key)
    if confirmed is None:
        audit.append(
            actor="local",
            tool="node",
            action="node.manifest_reconfirm",
            target="",
            decision="deny",
            result="no trusted confirmation: system values only",
        )
        if say is not None:
            say(
                "The manifest needs to be confirmed again on this device (nox node allow "
                "<group>). Until then Nox may only read system values here."
            )
        confirmed = MOST_RESTRICTIVE
    groups = {g: bool(current.groups.get(g) and confirmed.groups.get(g)) for g in GROUPS}
    max_risk = min(current.max_risk, confirmed.max_risk, key=_RISKS.index)
    trust = current.trust_core_for_high and confirmed.trust_core_for_high
    effective = NodeManifest(
        groups=groups,
        max_risk=max_risk,
        trust_core_for_high=trust,
        max_calls_per_hour=min(current.max_calls_per_hour, confirmed.max_calls_per_hour),
        max_high_per_hour=min(current.max_high_per_hour, confirmed.max_high_per_hour),
        # A folder counts only when it was confirmed here, spelled the same.
        folders=[f for f in current.folders if f in confirmed.folders],
        # Games only narrow: every one the file or the confirmation names counts.
        games=[*confirmed.games, *(g for g in current.games if g not in confirmed.games)],
    )
    widened = sorted(g for g in GROUPS if current.groups.get(g) and not groups[g])
    widened += [f"folder:{f}" for f in current.folders if f not in effective.folders]
    if confirmed is not MOST_RESTRICTIVE and widened:
        audit.append(
            actor="local",
            tool="node",
            action="node.manifest_unconfirmed",
            target="",
            decision="deny",
            result="not applied: " + ",".join(widened),
        )
        if say is not None:
            say(
                "node.yaml allows more than was confirmed on this device: "
                + ", ".join(widened)
                + ". Not applied. Confirm in a terminal: nox node allow <group>"
            )
    return effective
