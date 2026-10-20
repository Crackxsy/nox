"""Persistent, read-only credentials for the stream overlay (IPC role `overlay`).

The session token changes on every core start, which is right for the dashboard and the desktop
pet - the shell hands them the new one - but wrong for a browser source in OBS: OBS keeps the URL
it was given, so an overlay built on the session token broke with every restart of Nox.

An overlay credential survives restarts and is worth almost nothing to someone who copies it:
the `overlay` role may subscribe to the pet's events and read the public state snapshot, and
nothing else (`nox.ipc.dispatch.ROLE_ALLOWLIST`, `nox.ipc.server.OVERLAY_EVENTS`). Only its
SHA-256 is stored - the token itself is 256 random bits, so a fast hash is enough - in an
owner-only file next to the rest of Nox's data. The plaintext exists once, in the URL handed to
OBS; showing the URL again means issuing a new credential, which replaces the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nox.ipc._log import get_logger
from nox.ipc.tokens import generate_token, write_secret_file

log = get_logger(__name__)

OVERLAY_TOKENS_FILE = "overlay_tokens.json"  # noqa: S105 - a file name, not a secret
#: The label `stream.overlay.setup` issues under; one credential per label.
OBS_LABEL = "obs"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OverlayCredential:
    """One stored credential, without anything that would let it be used."""

    credential_id: str
    label: str
    created_at: str

    def public(self) -> dict[str, str]:
        return {"id": self.credential_id, "label": self.label, "created_at": self.created_at}


@dataclass(frozen=True)
class _Stored:
    credential: OverlayCredential
    digest: str


class OverlayTokenStore:
    """Issue, verify and revoke overlay credentials; persisted as hashes in one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: list[_Stored] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[_Stored]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # A damaged file means no overlay can connect until one is issued again - never that
            # an unknown token is accepted.
            log.warning("overlay_tokens_unreadable", path=str(self._path), error=str(exc))
            return []
        entries: list[_Stored] = []
        for item in raw.get("credentials", []) if isinstance(raw, dict) else []:
            try:
                entries.append(
                    _Stored(
                        OverlayCredential(
                            credential_id=str(item["id"]),
                            label=str(item["label"]),
                            created_at=str(item["created_at"]),
                        ),
                        str(item["sha256"]),
                    )
                )
            except (KeyError, TypeError):
                continue
        return entries

    def _save(self) -> None:
        data: dict[str, Any] = {
            "credentials": [{**e.credential.public(), "sha256": e.digest} for e in self._entries]
        }
        write_secret_file(self._path, json.dumps(data, indent=2))

    def issue(self, label: str = OBS_LABEL) -> tuple[OverlayCredential, str]:
        """A new credential for `label`, replacing (revoking) any earlier one with that label.
        Returns the credential and its token - the only time the token is ever available. The
        returned credential's id is what `adopt`/`revoke` take."""
        token = generate_token()
        credential = OverlayCredential(
            credential_id=uuid.uuid4().hex[:12],
            label=label,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._entries = [e for e in self._entries if e.credential.label != label]
            self._entries.append(_Stored(credential, _digest(token)))
            self._save()
        log.info("overlay_credential_issued", credential=credential.credential_id, label=label)
        return credential, token

    def verify(self, token: str) -> OverlayCredential | None:
        """The credential `token` belongs to, or None. Compares against every stored digest in
        constant time, without stopping at the first match."""
        if not token:
            return None
        digest = _digest(token)
        found: OverlayCredential | None = None
        with self._lock:
            for entry in self._entries:
                if hmac.compare_digest(entry.digest, digest):
                    found = entry.credential
        return found

    def adopt(self, credential_id: str, label: str) -> list[str]:
        """Give `credential_id` the label `label`, revoking any other credential that had it.
        Returns the revoked ids. Used to make a credential the overlay's only after the browser
        source actually points at it - until then the previous overlay keeps working."""
        with self._lock:
            target = next(
                (e for e in self._entries if e.credential.credential_id == credential_id), None
            )
            if target is None:
                raise KeyError(credential_id)
            doomed = [
                e.credential.credential_id
                for e in self._entries
                if e.credential.label == label and e is not target
            ]
            relabelled = _Stored(
                OverlayCredential(
                    credential_id=credential_id,
                    label=label,
                    created_at=target.credential.created_at,
                ),
                target.digest,
            )
            self._entries = [
                relabelled if e is target else e
                for e in self._entries
                if e.credential.credential_id not in doomed
            ]
            self._save()
        return doomed

    def revoke(self, credential_id: str) -> bool:
        with self._lock:
            kept = [e for e in self._entries if e.credential.credential_id != credential_id]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
        log.info("overlay_credential_revoked", credential=credential_id)
        return True

    def revoke_label(self, label: str) -> list[str]:
        """Revoke every credential with `label`; returns their ids."""
        with self._lock:
            doomed = [
                e.credential.credential_id for e in self._entries if e.credential.label == label
            ]
            if not doomed:
                return []
            self._entries = [e for e in self._entries if e.credential.label != label]
            self._save()
        log.info("overlay_credentials_revoked", label=label, count=len(doomed))
        return doomed

    def credentials(self) -> list[OverlayCredential]:
        with self._lock:
            return [e.credential for e in self._entries]
