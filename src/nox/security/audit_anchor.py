"""Where the audit log's head is remembered outside its own database.

A hash chain proves that the rows it has are consistent with each other. It cannot prove that rows
are *missing*: delete the newest rows, or the whole database, and what is left is a perfectly
valid, shorter chain. The anchor closes that gap. It records the head - the last sequence number
and its hash - somewhere the database cannot take along with it:

- `FileAuditAnchor`: a small JSON file, written atomically after every appended row. It lives in
  the runtime folder, named after a digest of the database path, so moving the database elsewhere
  starts a new anchor instead of accusing the new database of a truncation.
- `SecretAuditAnchor`: the same head in the credential store (`nox/security/audit_head`), written
  at boot and at shutdown only - the credential store is too slow, and on some systems too
  interactive, for every row. A machine without a credential store has the file anchor only, and
  the boot log says so.

At boot `SqliteAuditLog.verify_since_checkpoint` compares every anchor against the database: a head
behind the anchor (truncation, a deleted database) or a row at the anchor's position with another
hash (a rewrite) is a broken chain. An anchor that exists but cannot be read counts as broken too:
"cannot tell" is never "fine". No anchor at all is a fresh install.

This is tamper *evidence* against anything that edits the database on its own. Someone who can
rewrite the database, the runtime folder and the credential store together, as the same user, is
outside what it can detect (SECURITY.md, "Audit logging").
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nox.security._logging import get_logger
from nox.security.model import SecretStore
from nox.security.secrets import SecretStoreUnavailableError

log = get_logger(__name__)

__all__ = [
    "AUDIT_HEAD_SECRET",
    "AnchorUnavailableError",
    "AnchorUnreadableError",
    "AuditAnchor",
    "AuditHead",
    "FileAuditAnchor",
    "SecretAuditAnchor",
    "anchor_file_name",
    "database_key",
]

#: The credential-store entry holding the head (never a secret; the store is merely out of reach
#: of the database).
AUDIT_HEAD_SECRET = "nox/security/audit_head"  # noqa: S105 - an entry name, not a value

_HASH_RE = r"^[0-9a-f]{64}$"


class AuditHead(BaseModel):
    """The newest row of the chain: its sequence number and hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    hash: str = Field(pattern=_HASH_RE)


class _StoredHead(AuditHead):
    #: Digest of the database path the head belongs to (see `database_key`).
    database: str


class AnchorUnreadableError(RuntimeError):
    """An anchor exists but its content cannot be trusted. Treated as a broken chain."""


class AnchorUnavailableError(RuntimeError):
    """The anchor's storage cannot be reached at all (no credential store). Skipped, and logged."""


class AuditAnchor(Protocol):
    """One place the head is kept. `read` is None when nothing was ever anchored here."""

    @property
    def name(self) -> str: ...
    def read(self) -> AuditHead | None: ...
    def write(self, head: AuditHead) -> None: ...


def database_key(database: Path | str) -> str:
    """A short digest of the database path: ties an anchor to one database without storing the
    path itself. An in-memory database gets a fixed key."""
    text = str(database)
    if text != ":memory:":
        text = str(Path(text).resolve())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def anchor_file_name(database: Path | str) -> str:
    return f"audit_head-{database_key(database)}.json"


def _parse(raw: str, key: str, where: str) -> AuditHead | None:
    try:
        stored = _StoredHead.model_validate_json(raw)
    except ValidationError as exc:
        raise AnchorUnreadableError(f"{where}: {exc.errors()[0]['msg']}") from exc
    if stored.database != key:
        # The head of another database (the entry is per user, the database can move): not
        # evidence about this one.
        return None
    return AuditHead(seq=stored.seq, hash=stored.hash)


def _render(head: AuditHead, key: str) -> str:
    return json.dumps({"database": key, "seq": head.seq, "hash": head.hash}, sort_keys=True)


class FileAuditAnchor:
    """The head in a JSON file next to the runtime tokens, replaced atomically on every write."""

    def __init__(self, path: Path, *, database: Path | str) -> None:
        self.path = path
        self._key = database_key(database)

    @property
    def name(self) -> str:
        return "file"

    def read(self) -> AuditHead | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AnchorUnreadableError(f"anchor file: {type(exc).__name__}") from exc
        return _parse(raw, self._key, "anchor file")

    def write(self, head: AuditHead) -> None:
        """Write a sibling temp file, then rename it over the old one.

        A crash of the process leaves either the old anchor or the new one, never half of one.
        There is deliberately no fsync: the database commits without one too (WAL, synchronous
        NORMAL), and an anchor that is more durable than the rows it describes would, after a
        power cut, be *ahead* of them and read as a truncation.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(_render(head, self._key), encoding="utf-8")
        os.replace(tmp, self.path)


class SecretAuditAnchor:
    """The head in the credential store. Unavailable (no store) is reported, never guessed."""

    def __init__(self, secrets: SecretStore, *, database: Path | str) -> None:
        self._secrets = secrets
        self._key = database_key(database)

    @property
    def name(self) -> str:
        return "credential_store"

    def read(self) -> AuditHead | None:
        raw = self._call(lambda: self._secrets.get(AUDIT_HEAD_SECRET))
        if raw is None:
            return None
        return _parse(raw, self._key, "credential store anchor")

    def write(self, head: AuditHead) -> None:
        self._call(lambda: self._secrets.set(AUDIT_HEAD_SECRET, _render(head, self._key)))

    @staticmethod
    def _call[T](fn: Callable[[], T]) -> T:
        """Any failure of the store itself - no backend, a locked keychain, a refused write - is
        "cannot reach it", never a verdict about the chain."""
        try:
            return fn()
        except SecretStoreUnavailableError as exc:
            raise AnchorUnavailableError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - backend-specific keyring errors
            raise AnchorUnavailableError(f"credential store error: {type(exc).__name__}") from exc
