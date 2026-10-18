"""Certificates for the network listener: a certificate authority of this installation's own and a
server certificate it signs.

Why an own authority rather than one self-signed server certificate: a device that installs the
authority once trusts every server certificate Nox issues later - after the Pi got a new address,
say - without a second warning, and only then may a phone install the dashboard as an app.

What keeps that authority harmless:
* **Name constraints.** It may only vouch for `localhost`, names ending in `.local`, this machine's
  own host name and private or loopback addresses. A phone that trusts it does not trust it for
  any site on the internet, even if its key were stolen; browsers and phones enforce the
  constraint, not Nox.
* **The key never leaves this machine** and is written owner-only (`write_secret_file`).
* EC P-256 keys, SHA-256 signatures. The authority is valid for ten years, a server certificate
  for 825 days (the longest Apple devices accept). A server certificate is re-issued 30 days
  before it runs out, when this machine's names or addresses change, or when it was not signed by
  the current authority.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nox.core.logging import get_logger
from nox.ipc.tokens import write_secret_file

log = get_logger(__name__)

__all__ = [
    "CA_VALIDITY_DAYS",
    "KeyNotProtectedError",
    "PERMITTED_NETWORKS",
    "SERVER_VALIDITY_DAYS",
    "TlsMaterial",
    "ensure_tls",
    "fingerprint",
    "local_identity",
    "permitted_address",
]

CA_VALIDITY_DAYS = 3650
SERVER_VALIDITY_DAYS = 825
RENEW_BEFORE = dt.timedelta(days=30)

CA_KEY = "ca.key"
CA_CERT = "ca.crt"
SERVER_KEY = "server.key"
SERVER_CERT = "server.crt"

#: The addresses the authority may vouch for: loopback, the private IPv4 ranges, link-local and
#: unique-local IPv6. Nothing routable on the internet.
PERMITTED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
#: DNS names it may vouch for, and every name below them (`local` covers `pi.local`).
PERMITTED_DNS: tuple[str, ...] = ("localhost", "local")


class KeyNotProtectedError(RuntimeError):
    """A private key could not be made owner-only. Nothing listens with such a key: the file is
    removed again and the network listener stays off (fail closed)."""


@dataclass(frozen=True)
class TlsMaterial:
    """What the listener and the pairing page need. No private key material."""

    cert_file: Path
    key_file: Path
    ca_file: Path
    ca_der: bytes
    #: SHA-256 of the authority as `AB:CD:...`, what a person compares with the browser's view.
    fingerprint: str
    names: tuple[str, ...]
    addresses: tuple[str, ...]
    #: The server certificate's serial number: a new one means the listener must reload.
    server_serial: int = 0


def fingerprint(der: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    return ":".join(f"{b:02X}" for b in digest.finalize())


# ---- this machine's names and addresses --------------------------------------------------------


def _primary_ipv4() -> str | None:
    """The address this machine uses towards the network. A UDP "connect" sends nothing; it only
    asks the routing table which interface would be used (192.0.2.1 is a documentation address)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def permitted_address(value: str) -> bool:
    """Whether the authority may vouch for this address (loopback or private only)."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in PERMITTED_NETWORKS)


def local_identity() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(names, addresses) under which devices in the home network may reach this machine.

    Names: `localhost`, the host name and `<host name>.local` (mDNS, which Raspberry Pi OS,
    macOS, iOS, Android and Windows resolve). Addresses: loopback plus every private address of
    this machine. Public addresses are left out on purpose: the authority may not vouch for them.
    """
    host = socket.gethostname().strip().lower().rstrip(".")
    short = host.split(".", 1)[0]
    names = ["localhost"]
    if short and short != "localhost":
        names += [short, f"{short}.local"]
    candidates: list[str] = []
    primary = _primary_ipv4()
    if primary:
        candidates.append(primary)
    try:
        candidates += [str(info[4][0]) for info in socket.getaddrinfo(host, None, socket.AF_INET)]
    except OSError:
        pass
    addresses = ["127.0.0.1"]
    addresses += [c for c in dict.fromkeys(candidates) if permitted_address(c) and c != "127.0.0.1"]
    return tuple(dict.fromkeys(names)), tuple(addresses)


# ---- files -------------------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _restrict_dir(directory: Path) -> None:
    """The folder holding the keys is owner-only on POSIX (0700). On Windows each key file carries
    its own owner-only ACL (`write_secret_file`)."""
    if sys.platform != "win32":
        os.chmod(directory, 0o700)


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    if not write_secret_file(path, pem.decode("ascii")):
        path.unlink(missing_ok=True)
        log.error("lan.tls_key_not_restricted", file=path.name)
        raise KeyNotProtectedError(f"{path.name} could not be made readable by this user only")


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load_pair(
    key_file: Path, cert_file: Path
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None:
    """A key and the certificate that belongs to it, or None when either is missing, unreadable
    or they do not match."""
    if not (key_file.is_file() and cert_file.is_file()):
        return None
    try:
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    except ValueError as exc:
        log.warning("lan.tls_unreadable", file=cert_file.name, error=str(exc))
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        return None
    if cert.public_key() != key.public_key():
        log.warning("lan.tls_key_mismatch", file=cert_file.name)
        return None
    return key, cert


def _expiring(cert: x509.Certificate) -> bool:
    return cert.not_valid_after_utc - RENEW_BEFORE <= _now()


# ---- issuing -----------------------------------------------------------------------------------


def _key_usage(*, sign_certificates: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not sign_certificates,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=sign_certificates,
        crl_sign=sign_certificates,
        encipher_only=False,
        decipher_only=False,
    )


def _constraint_subtrees(host_names: Iterable[str]) -> list[x509.GeneralName]:
    dns = list(PERMITTED_DNS)
    dns += [n for n in host_names if "." not in n and n not in dns]
    subtrees: list[x509.GeneralName] = [x509.DNSName(name) for name in dns]
    subtrees += [x509.IPAddress(network) for network in PERMITTED_NETWORKS]
    return subtrees


def _new_ca(
    directory: Path, host_names: Sequence[str]
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    label = next((n for n in host_names if n != "localhost" and "." not in n), "Nox")
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Nox"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"Nox {label} - nur Heimnetz"),
        ]
    )
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(sign_certificates=True), critical=True)
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=_constraint_subtrees(host_names), excluded_subtrees=None
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(directory / CA_KEY, key)
    _write_cert(directory / CA_CERT, cert)
    # A new authority invalidates every server certificate the old one signed.
    (directory / SERVER_CERT).unlink(missing_ok=True)
    log.info(
        "lan.tls_ca_created", fingerprint=fingerprint(cert.public_bytes(serialization.Encoding.DER))
    )
    return key, cert


def _names_within(ca: x509.Certificate, names: Sequence[str]) -> list[str]:
    """The names the authority's constraints allow. A host name it was not made for (the machine
    was renamed) is dropped; its `.local` name and its addresses still reach it."""
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    allowed = [g.value for g in constraints.permitted_subtrees or [] if isinstance(g, x509.DNSName)]
    return [n for n in names if any(n == a or n.endswith("." + a) for a in allowed)]


def _server_current(
    pair: tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None,
    ca: x509.Certificate,
    names: Sequence[str],
    addresses: Sequence[str],
) -> bool:
    if pair is None:
        return False
    _key, cert = pair
    try:
        cert.verify_directly_issued_by(ca)
    except (ValueError, TypeError, InvalidSignature):
        return False
    if _expiring(cert):
        return False
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    have_names = set(san.get_values_for_type(x509.DNSName))
    have_addresses = {str(a) for a in san.get_values_for_type(x509.IPAddress)}
    return have_names == set(names) and have_addresses == set(addresses)


def _new_server(
    directory: Path,
    ca_key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    names: Sequence[str],
    addresses: Sequence[str],
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    alt: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    alt += [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    common = next((n for n in names if n.endswith(".local")), "localhost")
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=SERVER_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(sign_certificates=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(directory / SERVER_KEY, key)
    _write_cert(directory / SERVER_CERT, cert)
    log.info("lan.tls_server_issued", names=list(names), addresses=list(addresses))


def ensure_tls(
    directory: Path,
    *,
    names: Sequence[str] | None = None,
    addresses: Sequence[str] | None = None,
) -> TlsMaterial:
    """Create or reuse the authority and a current server certificate in `directory`.

    `names`/`addresses` default to `local_identity()`. Anything the authority may not vouch for
    (a public address, a name outside its constraints) is dropped rather than issued.
    """
    if names is None or addresses is None:
        found_names, found_addresses = local_identity()
        names = found_names if names is None else names
        addresses = found_addresses if addresses is None else addresses
    wanted_names = [n.lower().rstrip(".") for n in dict.fromkeys(names) if n]
    wanted_addresses = [a for a in dict.fromkeys(addresses) if permitted_address(a)]
    directory.mkdir(parents=True, exist_ok=True)
    _restrict_dir(directory)

    authority = _load_pair(directory / CA_KEY, directory / CA_CERT)
    if authority is None or _expiring(authority[1]):
        authority = _new_ca(directory, wanted_names)
    ca_key, ca = authority
    issued_names = _names_within(ca, wanted_names)
    if not _server_current(
        _load_pair(directory / SERVER_KEY, directory / SERVER_CERT),
        ca,
        issued_names,
        wanted_addresses,
    ):
        _new_server(directory, ca_key, ca, issued_names, wanted_addresses)
    der = ca.public_bytes(serialization.Encoding.DER)
    served = x509.load_pem_x509_certificate((directory / SERVER_CERT).read_bytes())
    return TlsMaterial(
        cert_file=directory / SERVER_CERT,
        key_file=directory / SERVER_KEY,
        ca_file=directory / CA_CERT,
        ca_der=der,
        fingerprint=fingerprint(der),
        names=tuple(issued_names),
        addresses=tuple(wanted_addresses),
        server_serial=served.serial_number,
    )
