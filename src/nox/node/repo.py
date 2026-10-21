"""Storage for nodes: their details, every certificate issued to them, their capability groups.

The pairing row itself lives in `paired_devices` (channel `node`, `nox.remote.repo`); this
repository adds the tables of migration 0017. Revoking a node revokes every certificate it ever
had, so a copy of an old certificate is as dead as the current one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nox.data.db import Database

__all__ = ["NodeCertRow", "NodeRepository", "NodeRow"]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class NodeRow:
    device_id: str
    os: str
    arch: str
    version: str
    linked_device_id: str | None
    manifest_hash: str
    created_at: str


@dataclass(frozen=True)
class NodeCertRow:
    serial: str
    device_id: str
    spki_sha256: str
    issued_at: str
    not_after: str
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class NodeRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- nodes ---------------------------------------------------------------------------------

    def create_node(
        self, device_id: str, *, os: str, arch: str, version: str, when: datetime
    ) -> None:
        self._db.execute(
            "INSERT INTO nodes (device_id, os, arch, version, created_at) VALUES (?, ?, ?, ?, ?)",
            (device_id, os, arch, version, _iso(when)),
        )

    def get_node(self, device_id: str) -> NodeRow | None:
        row = self._db.fetch_one("SELECT * FROM nodes WHERE device_id = ?", (device_id,))
        if row is None:
            return None
        return NodeRow(
            device_id=str(row["device_id"]),
            os=str(row["os"]),
            arch=str(row["arch"]),
            version=str(row["version"]),
            linked_device_id=row["linked_device_id"],
            manifest_hash=str(row["manifest_hash"]),
            created_at=str(row["created_at"]),
        )

    def update_hello(self, device_id: str, *, version: str, manifest_hash: str) -> None:
        self._db.execute(
            "UPDATE nodes SET version = ?, manifest_hash = ? WHERE device_id = ?",
            (version, manifest_hash, device_id),
        )

    def set_linked_device(self, device_id: str, linked_device_id: str | None) -> None:
        self._db.execute(
            "UPDATE nodes SET linked_device_id = ? WHERE device_id = ?",
            (linked_device_id, device_id),
        )

    # -- certificates --------------------------------------------------------------------------

    def record_cert(
        self,
        *,
        serial: str,
        device_id: str,
        spki_sha256: str,
        issued_at: datetime,
        not_after: datetime,
    ) -> None:
        self._db.execute(
            "INSERT INTO node_certs (serial, device_id, spki_sha256, issued_at, not_after)"
            " VALUES (?, ?, ?, ?, ?)",
            (serial, device_id, spki_sha256, _iso(issued_at), _iso(not_after)),
        )

    def get_cert(self, serial: str) -> NodeCertRow | None:
        row = self._db.fetch_one("SELECT * FROM node_certs WHERE serial = ?", (serial,))
        if row is None:
            return None
        return NodeCertRow(
            serial=str(row["serial"]),
            device_id=str(row["device_id"]),
            spki_sha256=str(row["spki_sha256"]),
            issued_at=str(row["issued_at"]),
            not_after=str(row["not_after"]),
            revoked_at=row["revoked_at"],
        )

    def revoke_certs(self, device_id: str, *, reason: str, when: datetime) -> int:
        cursor = self._db.execute(
            "UPDATE node_certs SET revoked_at = ?, revoked_reason = ?"
            " WHERE device_id = ? AND revoked_at IS NULL",
            (_iso(when), reason, device_id),
        )
        return int(cursor.rowcount)

    def revoked_serials(self) -> set[str]:
        rows = self._db.fetch_all("SELECT serial FROM node_certs WHERE revoked_at IS NOT NULL")
        return {str(r["serial"]) for r in rows}

    # -- capability groups ---------------------------------------------------------------------

    def set_reported_groups(
        self, device_id: str, groups: dict[str, bool], *, when: datetime
    ) -> None:
        """What the node says it has enabled locally. The core's narrowing is kept as it is."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE node_capabilities SET enabled_on_node = 0, updated_at = ?"
                " WHERE device_id = ?",
                (_iso(when), device_id),
            )
            for group, enabled in groups.items():
                conn.execute(
                    "INSERT INTO node_capabilities (device_id, grp, enabled_on_node, updated_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(device_id, grp) DO UPDATE SET"
                    " enabled_on_node = excluded.enabled_on_node, updated_at = excluded.updated_at",
                    (device_id, group, int(enabled), _iso(when)),
                )

    def narrow(self, device_id: str, group: str, narrowed: bool, *, when: datetime) -> None:
        self._db.execute(
            "INSERT INTO node_capabilities (device_id, grp, narrowed, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(device_id, grp) DO UPDATE SET"
            " narrowed = excluded.narrowed, updated_at = excluded.updated_at",
            (device_id, group, int(narrowed), _iso(when)),
        )

    def effective_groups(self, device_id: str) -> set[str]:
        """Enabled on the node *and* not narrowed on the core - both have to agree."""
        rows = self._db.fetch_all(
            "SELECT grp FROM node_capabilities"
            " WHERE device_id = ? AND enabled_on_node = 1 AND narrowed = 0",
            (device_id,),
        )
        return {str(r["grp"]) for r in rows}
