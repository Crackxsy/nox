"""`nox-node pair`: the pairing with a core and the confirmation of the defaults a device starts
with."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from nox.node.device.cli.runtime import NodeCliError, open_audit, refuse_during_session
from nox.node.device.cli_text import (
    command,
    t,
)
from nox.node.device.confirmed import (
    manifest_key,
    record_confirmed,
)
from nox.node.device.enroll import EnrollError, enroll
from nox.node.device.home import NodeHome
from nox.node.device.keystore import NodeKeyStore
from nox.node.wire.manifest import GROUPS, NodeManifest, load_manifest, save_manifest
from nox.node.wire.protocol import PairLink, PairLinkError
from nox.security.model import SecretStore

#: Pairing refusals (`EnrollError.reason`) with a sentence of their own (`cli_text`).
_REASONS = frozenset(
    {
        "fingerprint_mismatch",
        "unreachable",
        "already_paired",
        "used",
        "expired",
        "unknown_code",
        "max_devices",
        "rate_limited",
        "name_taken",
        "invalid_characters",
        "not_nox",
        "key_not_protected",
    }
)


def _pair_reason(reason: str) -> str:
    if reason in _REASONS:
        return t(reason, again=command('pair --again "<link>"'))
    return t("pair_failed", reason=reason)


def again_reason(home: NodeHome, *, now: datetime | None = None) -> str | None:
    """Why this device may pair again with its old pairing put aside (`pair --again`): its
    certificate ran out, the core refused it (removed while offline), or the stored authority no
    longer matches the pin - or None: the pairing is fine and stays."""
    from cryptography import x509  # noqa: PLC0415

    identity = home.load_identity()
    if identity is None:
        return None
    if not home.pinned_ca_ok(identity):
        return "pin_mismatch"
    state = home.read_status().get("state")
    if state in ("refused", "removed", "pin_mismatch"):
        return str(state)
    try:
        cert = x509.load_pem_x509_certificate(home.cert_file.read_bytes())
    except (OSError, ValueError):
        return "certificate_unreadable"
    if cert.not_valid_after_utc <= (now or datetime.now(UTC)):
        return "expired"
    return None


def pair(
    text: str,
    *,
    home: NodeHome,
    name: str = "",
    secrets: SecretStore | None = None,
    again: bool = False,
) -> str:
    """Pair this device. With `again`, a pairing that cannot work any more (`again_reason`) is
    put aside first - key, certificate, authority and identity; the audit trail and the
    confirmation key stay - instead of deleting files by hand."""
    try:
        link = PairLink.parse(text)
    except PairLinkError as exc:
        raise NodeCliError(t("pair_bad_link", error=exc)) from exc
    keystore = NodeKeyStore(home.key_file, secrets)
    notes: list[str] = []
    if again:
        if home.load_identity() is None:
            raise NodeCliError(t("pair_again_not_paired", pair=command('pair "<link>"')))
        why = again_reason(home)
        if why is None:
            raise NodeCliError(t("pair_again_not_needed", again=command('pair --again "<link>"')))
        refuse_during_session(home)
        keystore.delete()
        home.forget()
        open_audit(home).append(
            actor="local",
            tool="node",
            action="node.pair_again",
            target="",
            decision="allow",
            result=why[:64],
        )
        notes.append(t("pair_again_ok", why=why))
    try:
        identity = asyncio.run(enroll(link, home, keystore, name=name))
    except EnrollError as exc:
        raise NodeCliError(_pair_reason(exc.reason)) from exc
    # The defaults, written and recorded as what this device confirmed at pairing.
    confirm_defaults(home, secrets)
    return "\n".join([*notes, t("pair_ok", name=identity.name, run=command("run"))])


def confirm_defaults(home: NodeHome, secrets: SecretStore | None) -> NodeManifest:
    """At pairing only the defaults are confirmed - never whatever a `node.yaml` that was there
    before says. What the file widened is dropped from it."""
    found = load_manifest(home.manifest_file)
    defaults = NodeManifest()
    start = NodeManifest(
        groups={g: bool(found.groups.get(g) and defaults.groups[g]) for g in GROUPS},
        max_risk=min(
            found.max_risk, defaults.max_risk, key=("read", "low", "medium", "high").index
        ),
        trust_core_for_high=False,
        games=found.games,  # watching more games only narrows
    )
    save_manifest(home.manifest_file, start)
    if not record_confirmed(
        open_audit(home), start, manifest_key(home.path, secrets), by="pairing", create=True
    ):
        raise NodeCliError(t("no_confirmation_key"))
    return start
