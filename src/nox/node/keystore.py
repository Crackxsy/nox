"""The node's private key: generated on this device, never sent anywhere.

Python's TLS can only load a key from a file, so the key lives in `node.key` (owner-only). Where
the operating system has a keyring - Windows Credential Manager (DPAPI), the macOS Keychain, a
desktop Linux secret service - the file is encrypted (PKCS#8, AES-256) with a random passphrase
kept in that keyring, so a copy of the folder alone is useless. A headless Linux node has no
keyring: the file is then unencrypted and protected by its 0600 mode only (documented residual
risk, `docs/NODES.md`).
"""

from __future__ import annotations

import contextlib
import secrets
import ssl
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import CertificateSigningRequestBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID

from nox.ipc.tokens import write_secret_file
from nox.security.model import SecretStore

__all__ = ["PASSPHRASE_SECRET", "KeyNotProtectedError", "NodeKeyStore"]

PASSPHRASE_SECRET = "nox/node/key_passphrase"  # noqa: S105 - the secret's name, not a secret


class KeyNotProtectedError(RuntimeError):
    """The key file could not be made owner-only; it is removed again (fail closed)."""


class NodeKeyStore:
    def __init__(self, key_file: Path, secret_store: SecretStore | None) -> None:
        self._key_file = key_file
        self._secrets = secret_store

    @property
    def encrypted(self) -> bool:
        return self._secrets is not None

    def create(self) -> ec.EllipticCurvePrivateKey:
        key = ec.generate_private_key(ec.SECP256R1())
        if self._secrets is not None:
            passphrase = secrets.token_urlsafe(32)
            self._secrets.set(PASSPHRASE_SECRET, passphrase)
            encryption: serialization.KeySerializationEncryption = (
                serialization.BestAvailableEncryption(passphrase.encode("ascii"))
            )
        else:
            encryption = serialization.NoEncryption()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
        )
        if not write_secret_file(self._key_file, pem.decode("ascii")):
            self._key_file.unlink(missing_ok=True)
            raise KeyNotProtectedError("node.key could not be made readable by this user only")
        return key

    def _passphrase(self) -> bytes | None:
        if self._secrets is None:
            return None
        value = self._secrets.get(PASSPHRASE_SECRET)
        return value.encode("ascii") if value else None

    def load(self) -> ec.EllipticCurvePrivateKey:
        key = serialization.load_pem_private_key(
            self._key_file.read_bytes(), password=self._passphrase()
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("node.key is not an EC key")
        return key

    def csr(self, key: ec.EllipticCurvePrivateKey, name: str) -> str:
        """A request for `key`. The authority ignores everything in it but the key."""
        request = (
            CertificateSigningRequestBuilder()
            .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, (name or "node")[:64])]))
            .sign(key, hashes.SHA256())
        )
        return request.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def client_context(self, *, ca_file: Path, cert_file: Path) -> ssl.SSLContext:
        """TLS towards the core: only the pinned authority is trusted, TLS 1.3, our certificate."""
        context = ssl.create_default_context(cafile=str(ca_file))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        passphrase = self._passphrase()
        context.load_cert_chain(
            str(cert_file), str(self._key_file), password=passphrase if passphrase else None
        )
        return context

    def delete(self) -> None:
        self._key_file.unlink(missing_ok=True)
        if self._secrets is not None:
            with contextlib.suppress(Exception):
                self._secrets.delete(PASSPHRASE_SECRET)
