"""SQLite mirror of PM vault notes (`pm_items`, migration `0004_pm.sql`): a fast-query index
rebuilt from the vault, which always wins on conflict (Data Model "vault wins", ADR-006 layer 3).
"""

from __future__ import annotations

from typing import Any

from nox.data.db import Database
from nox.pm.models import Kind, WorkItem

_COLUMNS = (
    "id",
    "kind",
    "title",
    "status",
    "priority",
    "estimate",
    "epic_id",
    "project_id",
    "note_path",
    "note_hash",
    "created",
    "updated",
)


def _insert_sql(*, upsert: bool) -> str:
    """Built once from the fixed `_COLUMNS` tuple above (never user input) - not a dynamic
    query."""
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    sql = f"INSERT INTO pm_items ({columns}) VALUES ({placeholders})"  # noqa: S608
    if upsert:
        updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "id")
        sql += f" ON CONFLICT(id) DO UPDATE SET {updates}"  # noqa: S608
    return sql


_INSERT_SQL = _insert_sql(upsert=False)
_UPSERT_SQL = _insert_sql(upsert=True)


def _params(item: WorkItem) -> dict[str, Any]:
    return {c: getattr(item, c) for c in _COLUMNS}


def _row_to_item(row: Any) -> WorkItem:
    return WorkItem.model_validate(dict(row))


class PmIndex:
    """Typed repository over `pm_items` (ADR-006, Data Model L2). Every write is a full mirror
    refresh (`rebuild`) or a single upsert - there is no row deletion API beyond `rebuild`, since a
    note simply disappearing from the vault is how an item is removed (vault wins)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def rebuild(self, items: list[WorkItem]) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM pm_items")
            if items:
                conn.executemany(_INSERT_SQL, [_params(i) for i in items])

    def upsert(self, item: WorkItem) -> None:
        self._db.execute(_UPSERT_SQL, _params(item))

    def get(self, item_id: str) -> WorkItem | None:
        row = self._db.fetch_one("SELECT * FROM pm_items WHERE id = ?", (item_id,))
        return _row_to_item(row) if row is not None else None

    def list_items(
        self, *, kind: Kind | None = None, statuses: tuple[str, ...] | None = None
    ) -> list[WorkItem]:
        sql = "SELECT * FROM pm_items WHERE 1=1"
        params: dict[str, Any] = {}
        if kind is not None:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if statuses:
            placeholders = ", ".join(f":st{i}" for i in range(len(statuses)))
            sql += f" AND status IN ({placeholders})"
            params.update({f"st{i}": s for i, s in enumerate(statuses)})
        sql += " ORDER BY id"
        return [_row_to_item(row) for row in self._db.fetch_all(sql, params)]
