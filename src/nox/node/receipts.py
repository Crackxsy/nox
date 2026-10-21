"""Core receipts: the core signs every call with a side effect, the node checks the signature.

A receipt is an Ed25519 signature by the core's receipt key over exactly what the core decided:
`call_id`, the node it is for and the core it comes from, the tool, the SHA-256 of the canonical
(NFC) arguments, risk, who decided (`user`, `grant`, `profile`, `device`), whether the decision was
left to the device, whether the conversation held foreign content, when it was issued and its
deadline. The node refuses a side effect without a valid receipt, with a receipt for another node,
other arguments or another tool, issued in the future, valid for longer than `MAX_LIFETIME_S`,
after its deadline, or a second time (replay). Times are compared on the core's clock as measured
from its pings, so a jump of the node's own clock (NTP) neither opens nor closes the window.

What a receipt protects against: a confused or replayed call, a call altered on its way, a call
meant for another node, a call from anything but the core's key. What it does not: a fully
compromised core signs whatever it wants - which is why the node still applies its own gate, its
own confirmation for high risk and its own budget (spec C §1.6).

The private key lives next to the authority (`<data_dir>/tls/receipt.key`, owner-only); the node
learns the public key at pairing and pins it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from nox.ipc.tokens import write_secret_file

__all__ = [
    "MAX_LIFETIME_S",
    "ReceiptSigner",
    "ReceiptVerifier",
    "args_hash",
    "normalize_arguments",
    "receipt_message",
]

KEY_FILE = "receipt.key"
#: A receipt is valid for at most this long after it was issued.
MAX_LIFETIME_S = 60.0
#: Clock difference between core and node tolerated on top of the measured skew.
SKEW_TOLERANCE_S = 5.0


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_nfc(v) for v in value]
    return value


def normalize_arguments(arguments: Any) -> Any:
    """The arguments with every string in NFC: the one form that is hashed, shown and run."""
    return _nfc(arguments)


def args_hash(arguments: Any) -> str:
    """SHA-256 of the canonical, NFC-normalised arguments. Raises ValueError for text that is
    not valid Unicode (lone surrogates) - such a call cannot be signed or checked."""
    canonical = json.dumps(
        _nfc(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    try:
        data = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("arguments are not valid Unicode") from exc
    return hashlib.sha256(data).hexdigest()


def receipt_message(
    *,
    call_id: str,
    device_id: str,
    core_id: str,
    tool: str,
    arguments_hash: str,
    risk: str,
    decided_by: str,
    confirm_on_device: bool,
    tainted: bool,
    issued_at: float,
    deadline: float,
) -> bytes:
    fields = {
        "call_id": call_id,
        "device": device_id,
        "core": core_id,
        "tool": tool,
        "args": arguments_hash,
        "risk": risk,
        "decided_by": decided_by,
        "confirm_on_device": confirm_on_device,
        "tainted": tainted,
        "issued_at": round(issued_at, 3),
        "deadline": round(deadline, 3),
    }
    return ("nox-receipt-v2\n" + json.dumps(fields, sort_keys=True)).encode("utf-8")


class ReceiptSigner:
    """The core's receipt key: made once, kept owner-only, never sent anywhere."""

    def __init__(self, directory: Path) -> None:
        path = directory / KEY_FILE
        if path.is_file():
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ValueError("receipt.key is not an Ed25519 key")
            self._key = loaded
        else:
            self._key = Ed25519PrivateKey.generate()
            pem = self._key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            directory.mkdir(parents=True, exist_ok=True)
            if not write_secret_file(path, pem.decode("ascii")):
                path.unlink(missing_ok=True)
                raise PermissionError("receipt.key could not be made readable by this user only")

    @property
    def public_key(self) -> str:
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode("ascii")

    def sign(
        self,
        *,
        call_id: str,
        device_id: str,
        core_id: str,
        tool: str,
        arguments: Any,
        risk: str,
        decided_by: str,
        confirm_on_device: bool,
        tainted: bool,
        issued_at: float,
        deadline: float,
    ) -> str:
        message = receipt_message(
            call_id=call_id,
            device_id=device_id,
            core_id=core_id,
            tool=tool,
            arguments_hash=args_hash(arguments),
            risk=risk,
            decided_by=decided_by,
            confirm_on_device=confirm_on_device,
            tainted=tainted,
            issued_at=issued_at,
            deadline=deadline,
        )
        return base64.b64encode(self._key.sign(message)).decode("ascii")


class ReceiptVerifier:
    """The node's check of a receipt against the pinned public key, for this node and its core;
    remembers used call ids."""

    def __init__(
        self,
        public_key: str,
        *,
        device_id: str,
        core_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raw = base64.b64decode(public_key, validate=True)
        self._key = Ed25519PublicKey.from_public_bytes(raw)
        self._device_id = device_id
        self._core_id = core_id
        self._clock = clock
        self._used: dict[str, float] = {}
        #: core clock minus node clock, measured from the core's pings (`observe_core_time`).
        self.skew_s = 0.0

    def observe_core_time(self, core_ts: float) -> None:
        sample = core_ts - self._clock()
        self.skew_s = sample if self.skew_s == 0.0 else 0.8 * self.skew_s + 0.2 * sample

    def check(
        self,
        *,
        receipt: str | None,
        call_id: str,
        tool: str,
        arguments: Any,
        risk: str,
        decided_by: str,
        confirm_on_device: bool,
        tainted: bool,
        issued_at: float,
        deadline: float,
    ) -> str | None:
        """None when the receipt is valid (and is now used up), else the refusal reason."""
        if not receipt:
            return "receipt_missing"
        now_core = self._clock() + self.skew_s
        for stale, until in list(self._used.items()):
            if until < now_core - SKEW_TOLERANCE_S:
                del self._used[stale]
        if deadline < now_core - SKEW_TOLERANCE_S:
            return "receipt_expired"
        if issued_at > now_core + SKEW_TOLERANCE_S:
            return "receipt_from_the_future"
        if deadline - issued_at > MAX_LIFETIME_S or deadline < issued_at:
            return "receipt_too_long"
        if call_id in self._used:
            return "receipt_replayed"
        try:
            signature = base64.b64decode(receipt, validate=True)
            self._key.verify(
                signature,
                receipt_message(
                    call_id=call_id,
                    device_id=self._device_id,
                    core_id=self._core_id,
                    tool=tool,
                    arguments_hash=args_hash(arguments),
                    risk=risk,
                    decided_by=decided_by,
                    confirm_on_device=confirm_on_device,
                    tainted=tainted,
                    issued_at=issued_at,
                    deadline=deadline,
                ),
            )
        except (InvalidSignature, ValueError):
            return "receipt_invalid"
        self._used[call_id] = deadline
        return None
