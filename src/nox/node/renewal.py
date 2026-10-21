"""Renewing the node's certificate before it ends (spec C §1.2, step 4).

A node certificate is valid for 30 days. From day 20 (`RENEW_WINDOW_DAYS` left) the node asks its
core over the already authenticated connection (`nox/renew`) for a new one - for the same key, so
nothing secret moves. What comes back is accepted only when the pinned authority signed it, it
names this node, it is for this node's key and it does not end before the current one; then
it replaces `node.crt` atomically. A node that stayed offline past the end has to pair again
(bounded damage of a stolen laptop).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nox.lan.tls import node_id_of
from nox.node.protocol import RENEW_WINDOW_DAYS

__all__ = ["RenewalRefusedError", "accept_renewed", "due", "store_certificate"]


class RenewalRefusedError(ValueError):
    """The certificate the core sent is not one this node may use."""


def _spki(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def due(cert_file: Path, *, now: datetime | None = None) -> bool:
    """Whether the current certificate has `RENEW_WINDOW_DAYS` or fewer left (or is unreadable -
    then renewing is the only thing that can help, and the core decides)."""
    try:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    except (OSError, ValueError):
        return True
    left = cert.not_valid_after_utc - (now or datetime.now(UTC))
    return left <= timedelta(days=RENEW_WINDOW_DAYS)


def accept_renewed(
    cert_pem: str,
    *,
    ca_file: Path,
    current_file: Path,
    key: ec.EllipticCurvePrivateKey,
    node_id: str,
) -> x509.Certificate:
    """The new certificate, checked; RenewalRefusedError otherwise."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
    except (OSError, ValueError, UnicodeEncodeError) as exc:
        raise RenewalRefusedError("not a certificate") from exc
    try:
        cert.verify_directly_issued_by(ca)
    except Exception as exc:  # noqa: BLE001 - InvalidSignature, ValueError, TypeError: all "no"
        raise RenewalRefusedError("not signed by the pinned authority") from exc
    if node_id_of(cert) != node_id:
        raise RenewalRefusedError("not a certificate for this node")
    public = cert.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey) or _spki(public) != _spki(
        key.public_key()
    ):
        raise RenewalRefusedError("not for this node's key")
    try:
        current_end = x509.load_pem_x509_certificate(current_file.read_bytes()).not_valid_after_utc
    except (OSError, ValueError):
        current_end = None  # unreadable: any valid certificate from the pinned authority is better
    if current_end is not None and cert.not_valid_after_utc < current_end:
        raise RenewalRefusedError("ends before the current one")
    if cert.not_valid_before_utc > datetime.now(UTC) + timedelta(minutes=10):
        raise RenewalRefusedError("not valid yet")
    return cert


def store_certificate(cert_file: Path, cert_pem: str) -> None:
    """Replace `node.crt` in one step: a crash leaves either the old or the new file."""
    temporary = cert_file.with_name(cert_file.name + ".new")
    with temporary.open("w", encoding="ascii") as handle:
        handle.write(cert_pem)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, cert_file)
