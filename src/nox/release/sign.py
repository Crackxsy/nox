"""Signing a manifest inside Nox - for tests and tools working with throwaway keys.

Real releases are signed by `scripts/sign_release.py`, which is self-contained on purpose (it runs
as `python -I -S` with nothing but `cryptography` importable, because it holds the release key)
and writes the same format. The key is a PKCS#8 PEM block as `openssl genpkey -algorithm ed25519`
writes it, unencrypted. Errors here never quote the key text.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nox.release.keys import key_id_for
from nox.release.verify import SIGNATURE_SCHEMA

__all__ = ["SigningKeyError", "load_signing_key", "sign_manifest"]


class SigningKeyError(ValueError):
    """The signing key could not be used. The message never contains key material."""


def load_signing_key(text: str) -> Ed25519PrivateKey:
    data = text.strip().encode("ascii", errors="replace")
    if not data:
        raise SigningKeyError("the signing key is empty")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except TypeError:
        raise SigningKeyError(
            "the signing key is encrypted; CI needs the unencrypted PKCS#8 PEM"
        ) from None
    except ValueError:
        # `from None`: the parser's own exception could carry fragments of the input.
        raise SigningKeyError("the signing key is not a PEM private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError("the signing key is not an Ed25519 key")
    return key


def sign_manifest(manifest_bytes: bytes, key: Ed25519PrivateKey) -> bytes:
    """The detached signature file for exactly these bytes."""
    document = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "ed25519",
        "key_id": key_id_for(key.public_key()),
        "signature": base64.b64encode(key.sign(manifest_bytes)).decode("ascii"),
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
