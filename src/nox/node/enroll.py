"""`nox node pair`: enrolling this device with a core, pinning its authority from the link.

1. The pairing link (`nox-node://pair?h=&p=&c=&fp=`) names the core and carries the SHA-256 of
   its authority. The authority is fetched from `https://<h>:<p>/nox-ca.crt` and compared with
   that fingerprint *before anything is sent*. A look-alike in the home network has no matching
   authority, so it never sees the code.
2. Only then, over TLS verified against that authority, the code and a certificate request go to
   `/api/node/pair`. The key was generated here and never leaves this device.
3. The answer is checked: the certificate is for our key, the authority in it is the pinned one.
"""

from __future__ import annotations

import hashlib
import platform
import ssl
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nox import __version__
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import KeyNotProtectedError, NodeKeyStore
from nox.node.protocol import NodePairResponse, PairLink

__all__ = ["EnrollError", "enroll", "local_os"]

#: The authority is a few hundred bytes; anything much larger is not one.
MAX_CA_BYTES = 16 * 1024


class EnrollError(RuntimeError):
    """Pairing failed; `reason` is a stable token for the terminal's message."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def local_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _sha256_hex(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _base(link: PairLink) -> str:
    host = f"[{link.host}]" if ":" in link.host else link.host
    return f"https://{host}:{link.port}"


async def _pinned_authority(
    link: PairLink, transport: httpx.AsyncBaseTransport | None
) -> x509.Certificate:
    # Unverified on purpose: what comes back is only compared with the pin, never trusted.
    body = bytearray()
    async with httpx.AsyncClient(verify=False, transport=transport, timeout=10) as client:  # noqa: S501
        try:
            async with client.stream("GET", f"{_base(link)}/nox-ca.crt") as response:
                if response.status_code != 200:
                    raise EnrollError("unreachable", f"HTTP {response.status_code}")
                # Read no more than an authority can be: a hostile answer cannot fill memory.
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_CA_BYTES:
                        raise EnrollError("not_nox", "the answer is too large")
        except httpx.HTTPError as exc:
            raise EnrollError("unreachable", f"{type(exc).__name__}") from exc
    try:
        ca = x509.load_der_x509_certificate(bytes(body))
    except ValueError as exc:
        raise EnrollError("not_nox") from exc
    if _sha256_hex(ca.public_bytes(serialization.Encoding.DER)) != link.ca_fingerprint:
        raise EnrollError("fingerprint_mismatch")
    return ca


async def enroll(
    link: PairLink,
    home: NodeHome,
    keystore: NodeKeyStore,
    *,
    name: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    verify: ssl.SSLContext | None = None,
) -> NodeIdentity:
    """Pair this device; on success the key, certificate, authority and identity are in `home`.

    `transport`/`verify` are for tests (an in-process core); normally both are None."""
    if home.paired:
        raise EnrollError("already_paired")
    ca = await _pinned_authority(link, transport)
    ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode("ascii")
    context = verify or ssl.create_default_context(cadata=ca_pem)
    home.ensure()
    try:
        key = keystore.create()
    except KeyNotProtectedError as exc:
        raise EnrollError("key_not_protected", str(exc)) from exc
    body: dict[str, Any] = {
        "code": link.code,
        "csr": keystore.csr(key, name),
        "name": name[:64],
        "os": local_os(),
        "arch": (platform.machine() or "unknown")[:16].replace("-", "_"),
        "version": __version__,
    }
    try:
        async with httpx.AsyncClient(verify=context, transport=transport, timeout=15) as client:
            response = await client.post(f"{_base(link)}/api/node/pair", json=body)
    except httpx.HTTPError as exc:
        keystore.delete()
        raise EnrollError("unreachable", type(exc).__name__) from exc
    if response.status_code != 201:
        keystore.delete()
        try:
            reason = str(response.json().get("error", "refused"))[:32]
        except ValueError:
            reason = "refused"
        raise EnrollError(reason)
    try:
        answer = NodePairResponse.model_validate(response.json())
        cert = x509.load_pem_x509_certificate(answer.cert.encode("ascii"))
        answer_ca = x509.load_pem_x509_certificate(answer.ca_cert.encode("ascii"))
    except ValueError as exc:
        keystore.delete()
        raise EnrollError("bad_answer") from exc
    if (
        cert.public_key() != key.public_key()
        or _sha256_hex(answer_ca.public_bytes(serialization.Encoding.DER)) != link.ca_fingerprint
        or answer.core_id != link.ca_fingerprint
    ):
        keystore.delete()
        raise EnrollError("bad_answer")
    identity = NodeIdentity(
        node_id=answer.node_id,
        name=answer.name,
        core_host=link.host,
        core_port=link.port,
        node_port=answer.node_port,
        ca_fingerprint=link.ca_fingerprint,
        paired_at=datetime.now(UTC),
    )
    home.save_pairing(identity, cert_pem=answer.cert, ca_pem=ca_pem)
    return identity
