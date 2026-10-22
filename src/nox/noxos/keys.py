"""Public keys for remote maintenance: what the dashboard may hand to the root helper.

The same rules as the helper's own check (`os/pi-gen/stage-nox/03-hardening/files/
nox_os_helper.py`, which re-checks everything it is given; `tests/unit/nox_os/test_os_helper.py`
keeps the two in step): one key per line as `ssh-keygen` prints it, ed25519 or ECDSA (optionally
on a security key), no `authorized_keys` options in front, a short printable comment at most.
RSA and DSA are refused - new keys for this are made with `ssh-keygen -t ed25519`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass

__all__ = ["KEY_TYPES", "MAX_KEYS", "InvalidKeyError", "PublicKey", "parse_key", "parse_keys"]

MAX_KEYS = 8
MAX_KEY_LINE = 1024
MAX_COMMENT = 100
#: Accepted types, and the curve an ECDSA blob must name.
KEY_TYPES: dict[str, str | None] = {
    "ssh-ed25519": None,
    "sk-ssh-ed25519@openssh.com": None,
    "ecdsa-sha2-nistp256": "nistp256",
    "ecdsa-sha2-nistp384": "nistp384",
    "ecdsa-sha2-nistp521": "nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com": "nistp256",
}
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_COMMENT = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")


class InvalidKeyError(ValueError):
    """`reason` is a stable code the dashboard translates."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PublicKey:
    type: str
    blob: bytes
    comment: str = ""

    @property
    def line(self) -> str:
        data = base64.b64encode(self.blob).decode("ascii")
        return f"{self.type} {data}" + (f" {self.comment}" if self.comment else "")

    @property
    def fingerprint(self) -> str:
        """As `ssh-keygen -lf` shows it."""
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode("ascii")
        return "SHA256:" + digest.rstrip("=")

    def view(self) -> dict[str, str]:
        return {"type": self.type, "fingerprint": self.fingerprint, "comment": self.comment}


def _strings(blob: bytes, count: int) -> list[bytes]:
    out, pos = [], 0
    for _ in range(count):
        if pos + 4 > len(blob):
            raise InvalidKeyError("damaged", "the key is damaged")
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        pos += 4
        if length > len(blob) - pos:
            raise InvalidKeyError("damaged", "the key is damaged")
        out.append(blob[pos : pos + length])
        pos += length
    return out


def parse_key(line: str) -> PublicKey:
    text = line.strip()
    if not text or len(text) > MAX_KEY_LINE:
        raise InvalidKeyError("too_long", "a public key is one line of at most 1024 characters")
    if any(not (32 <= ord(c) < 127) for c in text):
        raise InvalidKeyError("not_ascii", "a public key is plain ASCII on one line")
    parts = text.split(None, 2)
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise InvalidKeyError("type", "only ed25519 and ECDSA public keys are accepted")
    kind, data = parts[0], parts[1]
    comment = parts[2].strip() if len(parts) == 3 else ""
    if not _BASE64.fullmatch(data):
        raise InvalidKeyError("damaged", "the key is damaged")
    try:
        blob = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidKeyError("damaged", "the key is damaged") from None
    fields = _strings(blob, 2)
    curve = KEY_TYPES[kind]
    if fields[0] != kind.encode("ascii"):
        raise InvalidKeyError("damaged", "the key's type does not match its contents")
    if curve is None and len(fields[1]) != 32:
        raise InvalidKeyError("damaged", "the key is damaged")
    if curve is not None and fields[1] != curve.encode("ascii"):
        raise InvalidKeyError("damaged", "the key's curve does not match its type")
    if comment and (len(comment) > MAX_COMMENT or not _COMMENT.fullmatch(comment)):
        raise InvalidKeyError(
            "comment", "the key's comment is too long or has characters it may not have"
        )
    return PublicKey(kind, blob, comment)


def parse_keys(text_or_lines: str | list[str]) -> list[PublicKey]:
    """Every non-empty line a key; duplicates (same key, any comment) once."""
    lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else text_or_lines
    keys: dict[bytes, PublicKey] = {}
    for line in lines:
        if line.strip():
            key = parse_key(line)
            keys.setdefault(key.blob, key)
    if len(keys) > MAX_KEYS:
        raise InvalidKeyError("too_many", f"at most {MAX_KEYS} public keys")
    return list(keys.values())
