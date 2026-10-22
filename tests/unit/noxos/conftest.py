"""Fictitious public keys: valid in shape (built here from fixed bytes), owned by nobody."""

from __future__ import annotations

import base64
import struct


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _key(kind: str, *fields: bytes) -> str:
    blob = _string(kind.encode()) + b"".join(_string(f) for f in fields)
    return f"{kind} {base64.b64encode(blob).decode()}"


ED25519 = _key("ssh-ed25519", bytes(range(32)))
ED25519_B = _key("ssh-ed25519", bytes(range(32, 64)))
ECDSA = _key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes(64))
SK_ED25519 = _key("sk-ssh-ed25519@openssh.com", bytes(32), b"ssh:")
RSA = _key("ssh-rsa", b"\x01\x00\x01", bytes(256))
