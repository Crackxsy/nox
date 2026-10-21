"""Client certificates for nodes: what the authority signs, what it refuses, and how a node
certificate is recognised. A node gets a key binding and nothing else it asked for."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nox.lan.tls import (
    NODE_CERT_VALIDITY_DAYS,
    CsrRefusedError,
    TlsMaterial,
    issue_client_certificate,
    node_id_of,
    spki_sha256,
)
from tests.unit.node.conftest import make_csr, make_key

NODE_ID = "a" * 32


def _cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def test_the_certificate_binds_the_key_to_the_node_and_nothing_else(
    tls: TlsMaterial, tmp_path: Path
) -> None:
    key = make_key()
    issued = issue_client_certificate(tmp_path / "tls", make_csr(key, extra=True), NODE_ID)
    cert = _cert(issued.cert_pem)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign is False and usage.digital_signature is True
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == []  # the CSR's evil.example is ignored
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [f"urn:nox:node:{NODE_ID}"]
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == f"node:{NODE_ID}"
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime <= dt.timedelta(days=NODE_CERT_VALIDITY_DAYS, minutes=5)
    assert issued.spki_sha256 == spki_sha256(key.public_key())
    assert issued.serial == f"{cert.serial_number:x}"
    ca = x509.load_pem_x509_certificate(tls.ca_file.read_bytes())
    cert.verify_directly_issued_by(ca)
    assert node_id_of(cert) == NODE_ID


def test_two_certificates_never_share_a_serial(tls: TlsMaterial, tmp_path: Path) -> None:
    a = issue_client_certificate(tmp_path / "tls", make_csr(make_key()), NODE_ID)
    b = issue_client_certificate(tmp_path / "tls", make_csr(make_key()), NODE_ID)
    assert a.serial != b.serial


def test_an_rsa_key_is_refused(tls: TlsMaterial, tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(CsrRefusedError):
        issue_client_certificate(tmp_path / "tls", make_csr(key), NODE_ID)


def test_another_curve_is_refused(tls: TlsMaterial, tmp_path: Path) -> None:
    with pytest.raises(CsrRefusedError):
        issue_client_certificate(
            tmp_path / "tls", make_csr(ec.generate_private_key(ec.SECP384R1())), NODE_ID
        )


def test_a_request_whose_signature_does_not_match_is_refused(
    tls: TlsMaterial, tmp_path: Path
) -> None:
    """One bit of the signature flipped: the request no longer proves possession of its key."""
    csr = x509.load_pem_x509_csr(make_csr(make_key()).encode())
    der = bytearray(csr.public_bytes(serialization.Encoding.DER))
    der[-3] ^= 0x01  # inside the last INTEGER of the ECDSA signature
    forged = x509.load_der_x509_csr(bytes(der))
    assert forged.is_signature_valid is False
    pem = forged.public_bytes(serialization.Encoding.PEM).decode()
    with pytest.raises(CsrRefusedError, match="signature"):
        issue_client_certificate(tmp_path / "tls", pem, NODE_ID)


def test_garbage_is_no_request(tls: TlsMaterial, tmp_path: Path) -> None:
    with pytest.raises(CsrRefusedError):
        issue_client_certificate(tmp_path / "tls", "-----BEGIN nonsense-----", NODE_ID)


def test_a_node_id_must_be_32_hex_digits(tls: TlsMaterial, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        issue_client_certificate(tmp_path / "tls", make_csr(make_key()), "../../etc")


def test_no_authority_no_certificate(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        issue_client_certificate(tmp_path / "empty", make_csr(make_key()), NODE_ID)


# ---- recognising a node certificate --------------------------------------------------------------


def _hand_made(
    *,
    eku: list[x509.ObjectIdentifier] | None = None,
    ca: bool = False,
    uris: list[str] | None = None,
    cn: str | None = None,
) -> x509.Certificate:
    key = make_key()
    now = dt.datetime.now(dt.UTC)
    node_uri = f"urn:nox:node:{NODE_ID}"
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn or f"node:{NODE_ID}")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")]))
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(eku if eku is not None else [ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(u) for u in (uris or [node_uri])]
            ),
            critical=False,
        )
    )
    return builder.sign(key, hashes.SHA256())


def test_a_well_formed_node_certificate_is_recognised() -> None:
    assert node_id_of(_hand_made()) == NODE_ID


@pytest.mark.parametrize(
    "cert",
    [
        pytest.param(lambda: _hand_made(eku=[ExtendedKeyUsageOID.SERVER_AUTH]), id="server-auth"),
        pytest.param(
            lambda: _hand_made(
                eku=[ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            id="both-auths",
        ),
        pytest.param(lambda: _hand_made(ca=True), id="a-ca"),
        pytest.param(lambda: _hand_made(uris=["urn:nox:device:" + NODE_ID]), id="not-a-node"),
        pytest.param(
            lambda: _hand_made(uris=[f"urn:nox:node:{NODE_ID}", "urn:nox:node:" + "b" * 32]),
            id="two-nodes",
        ),
        pytest.param(lambda: _hand_made(cn="node:" + "b" * 32), id="cn-disagrees"),
        pytest.param(lambda: _hand_made(uris=["urn:nox:node:ABC"]), id="bad-id"),
    ],
)
def test_anything_else_is_not_a_node(cert: object) -> None:
    assert node_id_of(cert()) is None  # type: ignore[operator]


def test_a_certificate_without_the_extensions_is_not_a_node(tls: TlsMaterial) -> None:
    ca = x509.load_pem_x509_certificate(tls.ca_file.read_bytes())
    assert node_id_of(ca) is None
