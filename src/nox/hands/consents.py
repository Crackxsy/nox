"""The consent store and the guard that `mic.record_long` must pass.

Recording a conversation that includes other people is lawful only with their consent (§ 201
StGB). The rule is enforced below the model: `has_consent(context)` is a database read, and
`mic.record_long` refuses when it returns False. The model cannot talk its way past an empty table.

A consent is per *context* - a stable key for the situation ("lesson:math-mr-weber") - so consent
given for one setting never leaks into another. Consent can be revoked without erasing the record
of what was once agreed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from nox.data.db import Database

__all__ = ["ConsentRepository", "ConsentRow"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ConsentRow:
    id: str
    context: str
    who: str
    wording: str
    granted_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class ConsentRepository:
    """Reads and writes the `consents` table (migration 0014)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def grant(self, *, context: str, who: str, wording: str) -> ConsentRow:
        context = context.strip()
        if not context or not who.strip() or not wording.strip():
            raise ValueError("a consent needs a context, who gave it, and the wording agreed")
        cid = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO consents (id, context, who, wording, granted_at) VALUES (?, ?, ?, ?, ?)",
            (cid, context, who.strip(), wording.strip(), _now_iso()),
        )
        row = self.get(cid)
        assert row is not None
        return row

    def get(self, consent_id: str) -> ConsentRow | None:
        row = self._db.fetch_one("SELECT * FROM consents WHERE id = ?", (consent_id,))
        return None if row is None else ConsentRow(**dict(row))

    def has_consent(self, context: str) -> bool:
        """Whether an un-revoked consent exists for exactly this context."""
        row = self._db.fetch_one(
            "SELECT 1 FROM consents WHERE context = ? AND revoked_at IS NULL LIMIT 1",
            (context.strip(),),
        )
        return row is not None

    def list_active(self) -> list[ConsentRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM consents WHERE revoked_at IS NULL ORDER BY granted_at DESC"
        )
        return [ConsentRow(**dict(r)) for r in rows]

    def revoke(self, context: str) -> int:
        cur = self._db.execute(
            "UPDATE consents SET revoked_at = ? WHERE context = ? AND revoked_at IS NULL",
            (_now_iso(), context.strip()),
        )
        return int(cur.rowcount)
