"""Device names: chosen on the device itself, so untrusted text.

One explicit rule instead of a race against look-alike tables: after NFKC normalisation (which
turns full-width letters into plain ones) a name may contain only `A-Z a-z 0-9 ä ö ü Ä Ö Ü ß`,
space and `- _ .`, at most `MAX_NAME` characters. Anything else - Cyrillic or Greek look-alikes,
control or bidi characters, emoji - is refused ("Bitte nur Buchstaben, Ziffern, Leerzeichen und
- _ ."). A name may not be a reserved word the router understands ("core", "all", ...) or look
like a node id, and it is unique in the household after case and umlaut folding ("Küche" and
"KUECHE" are one name). A name typed on the new device that collides is refused before the code
is spent; a fallback name is numbered ("Gerät 2").

Everything the model sees names a device as `label()`: "Laptop · 3f2a" - the id part cannot be
chosen by the device.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Iterable

__all__ = [
    "ALLOWED",
    "MAX_NAME",
    "RESERVED",
    "NameRejectedError",
    "display_name",
    "household_name",
    "label",
    "name_key",
]

MAX_NAME = 40
ALLOWED: frozenset[str] = frozenset(string.ascii_letters + string.digits + "äöüÄÖÜß -_.")
#: Words the router reads as "the core itself" or as "every device".
RESERVED: frozenset[str] = frozenset(
    {"core", "dieser core", "this core", "hier", "here", "all", "alle", "every", "none", "device"}
)
_NODE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPACES = re.compile(r"\s+")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


class NameRejectedError(ValueError):
    """A name typed on the new device that cannot be used; `reason` is a stable token:
    `invalid_characters` or `name_taken`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normal(name: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", name)).strip()


def _allowed_only(name: str) -> str:
    return _SPACES.sub(" ", "".join(c for c in name if c in ALLOWED)).strip()[:MAX_NAME].strip()


def name_key(name: str) -> str:
    """What uniqueness compares: case and umlauts folded, spaces collapsed."""
    return _SPACES.sub(" ", _normal(name).casefold().translate(_UMLAUTS)).strip()


def display_name(name: str) -> str:
    """A stored name as it is shown: allowed characters only, bounded, never empty."""
    return _allowed_only(_normal(name)) or "Gerät"


def label(name: str, device_id: str) -> str:
    """How the model and the dashboard name a device: its name plus a short id it cannot choose."""
    return f"{display_name(name)} · {device_id[:4]}"


def _usable(name: str) -> bool:
    return bool(name) and name.casefold() not in RESERVED and not _NODE_ID.match(name.casefold())


def household_name(
    wanted: str, taken: Iterable[str], *, fallback: str = "Gerät", strict: bool = False
) -> str:
    """The name a new device gets.

    `strict` (the name was typed on the new device): a character outside `ALLOWED` raises
    `NameRejectedError("invalid_characters")`, a name the household already has (after folding)
    `NameRejectedError("name_taken")`. Otherwise disallowed characters are dropped, an unusable
    name becomes `fallback`, and a taken fallback is numbered."""
    used = {name_key(t) for t in taken}
    normal = _normal(wanted)
    if strict and any(c not in ALLOWED for c in normal):
        raise NameRejectedError("invalid_characters")
    name = _allowed_only(normal)
    if _usable(name):
        if name_key(name) not in used:
            return name
        if strict:
            raise NameRejectedError("name_taken")
    else:
        name = _allowed_only(_normal(fallback)) or "Gerät"
    for n in range(1, 100):
        candidate = name if n == 1 else f"{name[: MAX_NAME - 4]} {n}"
        if name_key(candidate) not in used:
            return candidate
    raise NameRejectedError("name_taken")
