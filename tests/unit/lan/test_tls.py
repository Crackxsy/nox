"""The certificate authority and the server certificate (`nox.lan.tls`)."""

from __future__ import annotations

import ipaddress
import os
import ssl
import sys
from pathlib import Path

import pytest
from cryptography import x509

from nox.lan.tls import (
    CA_VALIDITY_DAYS,
    PERMITTED_NETWORKS,
    SERVER_VALIDITY_DAYS,
    TlsMaterial,
    ensure_tls,
    fingerprint,
    local_identity,
    permitted_address,
    permitted_name,
)
from tests.unit.lan.conftest import ADDRESSES, NAMES


def _cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _san(material: TlsMaterial) -> tuple[set[str], set[str]]:
    san = _cert(material.cert_file).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    names = set(san.value.get_values_for_type(x509.DNSName))
    addresses = {str(a) for a in san.value.get_values_for_type(x509.IPAddress)}
    return names, addresses


def test_the_server_certificate_names_this_machine(tls: TlsMaterial) -> None:
    names, addresses = _san(tls)
    assert names == set(NAMES)
    assert addresses == set(ADDRESSES)
    assert tls.names == NAMES and tls.addresses == ADDRESSES


def test_the_authority_has_an_english_name(tls: TlsMaterial) -> None:
    """What a phone shows in its trust settings - and what the guides tell people to look for."""
    ca = _cert(tls.ca_file)
    (name,) = ca.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    assert str(name.value).startswith("Nox ") and str(name.value).endswith(" home network CA")


def test_the_authority_may_only_vouch_for_the_home_network(tls: TlsMaterial) -> None:
    """Name constraints: a phone that trusts this authority does not trust it for the internet."""
    ca = _cert(tls.ca_file)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    permitted = constraints.value.permitted_subtrees or []
    dns = {g.value for g in permitted if isinstance(g, x509.DNSName)}
    networks = {g.value for g in permitted if isinstance(g, x509.IPAddress)}
    # No bare host name: a machine called "dev" would otherwise cover a real top-level domain.
    assert dns == {"localhost", "local", "home.arpa"}
    assert networks == {
        ipaddress.ip_network(n)
        for n in (
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        )
    }
    assert networks == set(PERMITTED_NETWORKS)
    basic = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca and basic.path_length == 0


def test_lifetimes(tls: TlsMaterial) -> None:
    ca, server = _cert(tls.ca_file), _cert(tls.cert_file)
    assert (ca.not_valid_after_utc - ca.not_valid_before_utc).days >= CA_VALIDITY_DAYS
    # Apple devices refuse TLS server certificates valid for longer than 825 days.
    assert (
        server.not_valid_after_utc - server.not_valid_before_utc
    ).days <= SERVER_VALIDITY_DAYS + 1


def test_public_addresses_and_foreign_names_are_never_issued(tmp_path: Path) -> None:
    material = ensure_tls(
        tmp_path / "tls",
        names=["localhost", "noxpi", "noxpi.local", "bank.example", "pi.home.arpa"],
        addresses=["127.0.0.1", "192.168.1.20", "8.8.8.8"],
    )
    names, addresses = _san(material)
    assert names == {"localhost", "noxpi.local", "pi.home.arpa"}
    assert addresses == {"127.0.0.1", "192.168.1.20"}


@pytest.mark.parametrize(
    ("address", "permitted"),
    [
        ("127.0.0.1", True),
        ("10.255.255.255", True),
        ("172.15.255.255", False),
        ("172.16.0.0", True),
        ("172.31.255.255", True),
        ("172.32.0.0", False),
        ("192.168.0.1", True),
        ("192.169.0.1", False),
        ("169.254.1.1", True),
        ("100.64.0.1", False),
        ("8.8.8.8", False),
        ("::1", True),
        ("fc00::1", True),
        ("fe80::1", True),
        ("2001:db8::1", False),
        ("::ffff:8.8.8.8", False),
        ("not-an-address", False),
    ],
)
def test_permitted_address_edges(address: str, permitted: bool) -> None:
    assert permitted_address(address) is permitted


@pytest.mark.parametrize(
    ("name", "permitted"),
    [
        ("localhost", True),
        ("pi.local", True),
        ("PI.LOCAL.", True),
        ("pi.home.arpa", True),
        ("local", True),
        ("pi", False),
        ("dev", False),
        ("fritz.box", False),
        ("pi.local.example.com", False),
        ("evil-local", False),
    ],
)
def test_permitted_name_edges(name: str, permitted: bool) -> None:
    assert permitted_name(name) is permitted


def test_the_router_name_is_used_when_it_is_a_home_network_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "noxpi")
    monkeypatch.setattr(socket, "getfqdn", lambda: "noxpi.home.arpa")
    names, _ = local_identity()
    assert names == ("localhost", "noxpi.local", "noxpi.home.arpa")
    monkeypatch.setattr(socket, "getfqdn", lambda: "noxpi.fritz.box")
    names, _ = local_identity()
    assert names == ("localhost", "noxpi.local")


def test_a_second_run_reuses_everything(tmp_path: Path) -> None:
    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    before = first.cert_file.read_bytes()
    second = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    assert second.cert_file.read_bytes() == before
    assert second.fingerprint == first.fingerprint


def test_a_new_address_reissues_the_server_certificate_only(tmp_path: Path) -> None:
    """A new DHCP lease: a new server certificate, the same authority - so a device that installed
    the authority sees no warning."""
    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    moved = ensure_tls(tmp_path / "tls", names=NAMES, addresses=("127.0.0.1", "192.168.1.21"))
    assert moved.fingerprint == first.fingerprint
    assert _san(moved)[1] == {"127.0.0.1", "192.168.1.21"}


def test_a_renamed_machine_is_reissued_under_the_same_authority(tmp_path: Path) -> None:
    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    renamed = ensure_tls(tmp_path / "tls", names=("localhost", "other.local"), addresses=ADDRESSES)
    assert renamed.names == ("localhost", "other.local")
    assert renamed.fingerprint == first.fingerprint
    assert renamed.server_serial != first.server_serial


def test_a_damaged_authority_is_replaced_and_invalidates_the_server_certificate(
    tmp_path: Path,
) -> None:
    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    (tmp_path / "tls" / "ca.key").write_text("not a key", encoding="utf-8")
    second = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    assert second.fingerprint != first.fingerprint
    _cert(second.cert_file).verify_directly_issued_by(_cert(second.ca_file))


def _handshake(ca_file: Path, cert_file: Path, key_file: Path, hostname: str) -> ssl.SSLObject:
    """A complete TLS handshake in memory, the client trusting only `ca_file`. Raises
    `ssl.SSLCertVerificationError` when the client refuses the server's certificate."""
    context = ssl.create_default_context(cafile=str(ca_file))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(cert_file), str(key_file))
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(client_in, client_out, server_hostname=hostname)
    peer = server.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(10):
        for side in (client, peer):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
    return client


def test_a_tls_client_trusting_the_authority_accepts_the_server(tls: TlsMaterial) -> None:
    """The chain is what a real TLS stack expects (Python's ssl, OpenSSL underneath)."""
    client = _handshake(tls.ca_file, tls.cert_file, tls.key_file, "noxpi.local")
    assert client.version() in ("TLSv1.2", "TLSv1.3")
    assert client.getpeercert()["subject"]


def test_a_tls_client_refuses_a_name_the_certificate_does_not_carry(tls: TlsMaterial) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls.ca_file, tls.cert_file, tls.key_file, "bank.example")


@pytest.mark.parametrize(
    ("dns", "address"),
    [("bank.example", None), (None, "8.8.8.8"), ("dev", None)],
    ids=["internet-name", "public-address", "bare-name"],
)
def test_a_real_tls_stack_enforces_the_name_constraints(
    tls: TlsMaterial, tmp_path: Path, dns: str | None, address: str | None
) -> None:
    """Even a certificate the authority itself signed - as a stolen key could - is refused for a
    name or address outside the home network. Not the extension's presence: OpenSSL's verdict."""
    import datetime as dt

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    ca_key = serialization.load_pem_private_key((tls.ca_file.parent / "ca.key").read_bytes(), None)
    ca = _cert(tls.ca_file)
    key = ec.generate_private_key(ec.SECP256R1())
    san: list[x509.GeneralName] = []
    if dns:
        san.append(x509.DNSName(dns))
    if address:
        san.append(x509.IPAddress(ipaddress.ip_address(address)))
    now = dt.datetime.now(dt.UTC)
    rogue = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns or str(address))]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )
    cert_file, key_file = tmp_path / "rogue.crt", tmp_path / "rogue.key"
    cert_file.write_bytes(rogue.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls.ca_file, cert_file, key_file, dns or str(address))


def test_the_fingerprints_are_those_of_the_authority_and_the_server(tls: TlsMaterial) -> None:
    from cryptography.hazmat.primitives import serialization

    assert tls.fingerprint == fingerprint(tls.ca_der)
    server_der = _cert(tls.cert_file).public_bytes(serialization.Encoding.DER)
    assert tls.server_fingerprint == fingerprint(server_der)
    assert tls.server_fingerprint != tls.fingerprint
    assert len(tls.fingerprint.split(":")) == 32


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits; Windows: the icacls test"
)
def test_keys_and_their_folder_are_owner_only(tls: TlsMaterial) -> None:
    assert os.stat(tls.key_file.parent).st_mode & 0o077 == 0
    assert os.stat(tls.key_file).st_mode & 0o077 == 0
    assert os.stat(tls.key_file.parent / "ca.key").st_mode & 0o077 == 0


def test_local_identity_offers_loopback_and_private_addresses_only() -> None:
    names, addresses = local_identity()
    assert "localhost" in names
    assert addresses[0] == "127.0.0.1"
    assert all(permitted_address(a) for a in addresses)


def test_a_key_that_cannot_be_protected_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: when the owner-only ACL cannot be applied, the key file is removed again and
    nothing is issued - the listener then stays off (`test_listener.py`)."""
    from nox.lan import tls as tls_module

    def unprotected(path: Path, content: str) -> bool:
        path.write_text(content, encoding="utf-8")
        return False

    monkeypatch.setattr(tls_module, "write_secret_file", unprotected)
    with pytest.raises(tls_module.KeyNotProtectedError):
        ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    assert not (tmp_path / "tls" / "ca.key").exists()
    assert not (tmp_path / "tls" / "server.key").exists()


def test_a_server_certificate_near_its_end_is_renewed_under_the_same_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime as dt

    from nox.lan import tls as tls_module

    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    real_now = tls_module._now
    # 20 days before the server certificate ends: inside the 30-day renewal window.
    later = real_now() + dt.timedelta(days=SERVER_VALIDITY_DAYS - 20)
    monkeypatch.setattr(tls_module, "_now", lambda: later)
    renewed = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    assert renewed.server_serial != first.server_serial
    assert renewed.fingerprint == first.fingerprint  # the same authority: no new warning


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_on_windows_the_key_is_readable_by_this_user_only(tls: TlsMaterial) -> None:
    import os
    import subprocess

    out = subprocess.run(  # noqa: S603 - fixed argv
        ["icacls", str(tls.key_file)], capture_output=True, text=True, check=True
    ).stdout
    # One access-control entry, for the current user; inheritance removed.
    entries = [line for line in out.splitlines() if ":(" in line]
    assert len(entries) == 1, out
    assert os.environ["USERNAME"].lower() in entries[0].lower()
