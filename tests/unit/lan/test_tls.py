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
    SERVER_VALIDITY_DAYS,
    TlsMaterial,
    ensure_tls,
    fingerprint,
    local_identity,
    permitted_address,
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


def test_the_authority_may_only_vouch_for_the_home_network(tls: TlsMaterial) -> None:
    """Name constraints: a phone that trusts this authority does not trust it for the internet."""
    ca = _cert(tls.ca_file)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    permitted = constraints.value.permitted_subtrees or []
    dns = {g.value for g in permitted if isinstance(g, x509.DNSName)}
    networks = {g.value for g in permitted if isinstance(g, x509.IPAddress)}
    assert dns == {"localhost", "local", "noxpi"}
    assert ipaddress.ip_network("192.168.0.0/16") in networks
    assert not any(ipaddress.ip_address("8.8.8.8") in n for n in networks)
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
        names=["localhost", "noxpi", "noxpi.local", "bank.example"],
        addresses=["127.0.0.1", "192.168.1.20", "8.8.8.8"],
    )
    names, addresses = _san(material)
    assert "bank.example" not in names
    assert "8.8.8.8" not in addresses
    assert permitted_address("10.1.2.3") and not permitted_address("1.1.1.1")


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


def test_a_renamed_machine_keeps_its_local_name(tmp_path: Path) -> None:
    ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    renamed = ensure_tls(
        tmp_path / "tls", names=("localhost", "neu", "neu.local"), addresses=ADDRESSES
    )
    # The authority was made for "noxpi"; "neu" is outside its constraints, "neu.local" is not.
    assert renamed.names == ("localhost", "neu.local")


def test_a_damaged_authority_is_replaced_and_invalidates_the_server_certificate(
    tmp_path: Path,
) -> None:
    first = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    (tmp_path / "tls" / "ca.key").write_text("not a key", encoding="utf-8")
    second = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    assert second.fingerprint != first.fingerprint
    _cert(second.cert_file).verify_directly_issued_by(_cert(second.ca_file))


def test_a_tls_client_trusting_the_authority_accepts_the_server(tls: TlsMaterial) -> None:
    """The chain is what a real TLS stack expects (Python's ssl, OpenSSL underneath)."""
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(tls.cert_file), str(tls.key_file))
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(client_in, client_out, server_hostname="noxpi.local")
    peer = server.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(10):
        for side in (client, peer):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
    assert client.version() is not None
    assert client.getpeercert()["subject"]


def test_the_fingerprint_is_the_sha256_of_the_authority(tls: TlsMaterial) -> None:
    assert tls.fingerprint == fingerprint(tls.ca_der)
    assert len(tls.fingerprint.split(":")) == 32


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_keys_and_their_folder_are_owner_only(tls: TlsMaterial) -> None:
    assert os.stat(tls.key_file.parent).st_mode & 0o077 == 0
    assert os.stat(tls.key_file).st_mode & 0o077 == 0
    assert os.stat(tls.key_file.parent / "ca.key").st_mode & 0o077 == 0


def test_local_identity_offers_loopback_and_private_addresses_only() -> None:
    names, addresses = local_identity()
    assert "localhost" in names
    assert addresses[0] == "127.0.0.1"
    assert all(permitted_address(a) for a in addresses)
