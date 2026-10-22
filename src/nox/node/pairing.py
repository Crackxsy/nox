"""Pairing a node on the core: a one-time code redeemed for a client certificate.

The code is the phone companion's (`nox.remote.pairing.PairingService`, channel `node`): eight
characters, five minutes, single use, only its salted hash stored, redeemed atomically. What a
redeemed code yields is a certificate signed by this installation's authority for the key the new
node generated itself (`nox.lan.tls.issue_client_certificate`). The private key never leaves the
node; the core keeps the hash of its public key (`paired_devices.public_key` as
`sha256(salt + "node:" + spki)`, and `node_certs.spki_sha256`) and the serial.

`identify(cert)` is what the node listener asks after every TLS handshake: does this certificate
belong to a paired, non-revoked node, under a serial that was issued to that node and never
revoked, for the key that was paired? Any "no" refuses the connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from nox.core.events import EventBus
from nox.core.logging import get_logger
from nox.lan.tls import (
    CsrRefusedError,
    IssuedClientCert,
    csr_spki_sha256,
    issue_client_certificate,
    node_id_of,
    spki_sha256,
)
from nox.node.names import NameRejectedError, household_name
from nox.node.protocol import NodePairRequest
from nox.node.repo import NodeRepository
from nox.remote.models import DeviceRow, PairingResult, PairingStart
from nox.remote.pairing import PairingService
from nox.remote.repo import RemoteRepository
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["CHANNEL", "NODE_ROLE", "NodePairing", "NodeRedeemed"]

CHANNEL = "node"
#: A node gets a new certificate at most this often (`renew`).
RENEW_MIN_INTERVAL = timedelta(hours=12)
NODE_ROLE = "node"

#: Called with (device_id, reason) after a revocation, so open connections close at once.
OnRevoked = Callable[[str, str], Awaitable[object]]


@dataclass(frozen=True)
class NodeRedeemed:
    result: PairingResult
    issued: IssuedClientCert | None = None


class NodePairing:
    def __init__(
        self,
        repo: RemoteRepository,
        nodes: NodeRepository,
        *,
        tls_dir: Path,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        ttl_s: float = 300.0,
        max_nodes: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._nodes = nodes
        self._tls_dir = tls_dir
        self._clock = clock or (lambda: datetime.now(UTC))
        self._on_revoked: list[OnRevoked] = []
        self.pairing = PairingService(
            repo,
            bus=bus,
            audit=audit,
            ttl_s=ttl_s,
            max_devices=max_nodes,
            channel=CHANNEL,
            role=NODE_ROLE,
            clock=self._clock,
        )

    def on_revoked(self, callback: OnRevoked) -> None:
        self._on_revoked.append(callback)

    async def start(self, *, name: str = "") -> PairingStart:
        return await self.pairing.start(device_name=name.strip()[:64])

    async def redeem(self, request: NodePairRequest) -> NodeRedeemed:
        """Check the certificate request first - a malformed one costs no code - then redeem the
        code for the request's key and sign it. A failure after the device row exists revokes
        that row again: no node exists without a certificate, none with two."""
        try:
            spki = csr_spki_sha256(request.csr)
        except CsrRefusedError as exc:
            log.warning("node.pair_csr_refused", reason=str(exc))
            return NodeRedeemed(PairingResult(ok=False, reason="bad_csr"))
        if request.name.strip() and self._code_valid(request.code):
            # A name typed on the new device that cannot be used costs no code either. Only
            # checked behind a valid code: without one, every request gets the same refusal, so
            # nobody can probe which names the household has.
            try:
                household_name(request.name, self._taken(), strict=True)
            except NameRejectedError as exc:
                log.warning("node.pair_name_refused", reason=exc.reason)
                return NodeRedeemed(PairingResult(ok=False, reason=exc.reason))
        result = await self.pairing.redeem(
            request.code, sender_id=spki, name=request.name, fallback_name=request.os.title()
        )
        if not result.ok:
            return NodeRedeemed(result)
        device_id = result.device_id
        # The name was typed on the new device: plain, bounded, never reserved, never another
        # device's name or id (`nox.node.names`).
        name = household_name(result.name, self._taken(but=device_id), fallback="Gerät")
        if name != result.name:
            self._nodes.rename(device_id, name)
            result = result.model_copy(update={"name": name})
        try:
            issued = await asyncio.to_thread(
                issue_client_certificate, self._tls_dir, request.csr, device_id
            )
            now = self._clock()
            self._nodes.create_node(
                device_id, os=request.os, arch=request.arch, version=request.version, when=now
            )
            self._nodes.record_cert(
                serial=issued.serial,
                device_id=device_id,
                spki_sha256=issued.spki_sha256,
                issued_at=now,
                not_after=issued.not_after,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: no half-paired node remains
            log.error("node.pair_issue_failed", error=f"{type(exc).__name__}: {exc}")
            await self.pairing.revoke(device_id, reason="issue_failed")
            return NodeRedeemed(PairingResult(ok=False, reason="issue_failed"))
        return NodeRedeemed(result, issued)

    def _code_valid(self, code: str) -> bool:
        """Whether `code` is a pending, unexpired node code - looked at, not spent."""
        row = self._repo.find_pairing(code.strip().upper(), channel=CHANNEL)
        if row is None or row["redeemed_at"] is not None:
            return False
        return datetime.fromisoformat(str(row["expires_at"])) > self._clock()

    def _taken(self, *, but: str = "") -> list[str]:
        """Every name and id a new device's name must not look like."""
        others = [d for d in self.nodes() if not d.revoked and d.id != but]
        return [n for d in others for n in (d.name, d.id)]

    def identify(self, cert: x509.Certificate) -> DeviceRow | None:
        """The paired node this (already chain-verified) certificate belongs to, or None."""
        node_id = node_id_of(cert)
        if node_id is None:
            return None
        row = self._nodes.get_cert(f"{cert.serial_number:x}")
        if row is None or row.revoked or row.device_id != node_id:
            return None
        public_key = cert.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            return None
        spki = spki_sha256(public_key)
        if spki != row.spki_sha256:
            return None
        device = self._repo.find_device_for_sender(CHANNEL, spki)
        if device is None or device.id != node_id or device.revoked or device.role != NODE_ROLE:
            return None
        self._repo.mark_seen(device.id, when=self._clock())
        return device

    async def renew(self, device_id: str, serial: str, csr: str) -> IssuedClientCert | None:
        """A new certificate for a connected node, before its current one (`serial`) ends.

        Only for the key that was paired: a request for another key is refused (a stolen
        certificate alone never yields a fresh one; a new key means pairing again). The node's
        pairing must still stand; the new serial is recorded like the first and the old one
        goes on the denylist. At most one renewal per `RENEW_MIN_INTERVAL` per node - whatever
        the number of connections."""
        device = self.get(device_id)
        current = self._nodes.get_cert(serial)
        if (
            device is None
            or device.revoked
            or current is None
            or current.revoked
            or current.device_id != device_id
        ):
            return None
        try:
            if csr_spki_sha256(csr) != current.spki_sha256:
                log.warning("node.renew_other_key", device_id=device_id)
                return None
        except CsrRefusedError as exc:
            log.warning("node.renew_csr_refused", reason=str(exc))
            return None
        last = self._nodes.last_issued_at(device_id)
        if last is not None and self._clock() - last < RENEW_MIN_INTERVAL:
            log.warning("node.renew_too_soon", device_id=device_id)
            return None
        issued = await asyncio.to_thread(issue_client_certificate, self._tls_dir, csr, device_id)
        now = self._clock()
        self._nodes.record_cert(
            serial=issued.serial,
            device_id=device_id,
            spki_sha256=issued.spki_sha256,
            issued_at=now,
            not_after=issued.not_after,
        )
        self._nodes.revoke_serial(serial, reason="renewed", when=now)
        return issued

    async def revoke(self, device_id: str, *, reason: str = "dashboard") -> bool:
        """Revoke the pairing and every certificate of the node, then close its connection."""
        ok = await self.pairing.revoke(device_id, reason=reason)
        if not ok:
            return False
        self._nodes.revoke_certs(device_id, reason=reason, when=self._clock())
        for callback in self._on_revoked:
            try:
                await callback(device_id, reason)
            except Exception as exc:  # noqa: BLE001 - one listener must not keep a node alive
                log.error("node.revoke_callback_failed", error=f"{type(exc).__name__}: {exc}")
        return True

    def nodes(self) -> list[DeviceRow]:
        return self.pairing.devices()

    def get(self, device_id: str) -> DeviceRow | None:
        device = self._repo.get_device(device_id)
        return device if device is not None and device.channel == CHANNEL else None
