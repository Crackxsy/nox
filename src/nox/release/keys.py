"""Release keys: the public half that verifies a manifest, and how a key is named and expires.

A key is written the way `openssl pkey -pubout` prints it - the base64 of its DER
SubjectPublicKeyInfo, the one line between the PEM markers - so the owner can paste it unchanged
and the bash installer can hand the same text to `openssl`. Its **key id** is the first 16 hex
characters of the SHA-256 of the raw 32-byte Ed25519 key; a signature names the key it was made
with, and the id is checked against the key, so a mislabelled entry cannot vouch for another.

A key is accepted up to and including its `not_after` day (UTC). Expiry bounds how long a key that
leaked without anyone noticing stays useful, and forces a rotation now and then, which keeps the
procedure practised.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

__all__ = [
    "KEY_ID_PATTERN",
    "TrustedKey",
    "describe_public_key",
    "key_id_for",
    "parse_public_key",
]

KEY_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
#: DER prefix of an Ed25519 SubjectPublicKeyInfo (RFC 8410): SEQUENCE, AlgorithmIdentifier
#: 1.3.101.112, BIT STRING of 33 bytes - followed by the 32 key bytes.
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_public_key(text: str) -> Ed25519PublicKey:
    """The Ed25519 key in `text`: a whole PEM block or just its base64 line.

    Raises ValueError for anything else - another key type included, so an RSA or P-256 key can
    never end up in the trust list by accident.
    """
    body = "".join(
        line.strip()
        for line in text.strip().splitlines()
        if line.strip() and not line.startswith("-----")
    )
    try:
        der = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64") from exc
    if len(der) != len(_ED25519_SPKI_PREFIX) + 32 or not der.startswith(_ED25519_SPKI_PREFIX):
        raise ValueError("not an Ed25519 public key (expected `openssl pkey -pubout` output)")
    key = serialization.load_der_public_key(der)
    if not isinstance(key, Ed25519PublicKey):  # pragma: no cover - the prefix check decides
        raise ValueError("not an Ed25519 public key")
    return key


def _raw(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id_for(key: Ed25519PublicKey) -> str:
    """First 16 hex characters of SHA-256 over the raw 32-byte key."""
    return hashlib.sha256(_raw(key)).hexdigest()[:16]


def describe_public_key(key: Ed25519PublicKey) -> tuple[str, str]:
    """(key id, base64 SubjectPublicKeyInfo) - the two values a trust-list entry needs."""
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key_id_for(key), base64.b64encode(der).decode("ascii")


@dataclass(frozen=True, slots=True)
class TrustedKey:
    """One entry of the trust list. Validated on construction: a broken entry fails loudly at
    import (and in CI), never silently at update time."""

    key_id: str
    public_key: str
    not_after: str

    def __post_init__(self) -> None:
        if not KEY_ID_PATTERN.match(self.key_id):
            raise ValueError(f"key id {self.key_id!r} is not 16 lowercase hex characters")
        if not _DATE.match(self.not_after):
            raise ValueError(f"not_after {self.not_after!r} is not YYYY-MM-DD")
        dt.date.fromisoformat(self.not_after)
        actual = key_id_for(parse_public_key(self.public_key))
        if actual != self.key_id:
            raise ValueError(f"key id {self.key_id} does not match its public key ({actual})")

    @property
    def expires(self) -> dt.date:
        return dt.date.fromisoformat(self.not_after)

    def valid_on(self, day: dt.date) -> bool:
        return day <= self.expires

    def load(self) -> Ed25519PublicKey:
        return parse_public_key(self.public_key)
