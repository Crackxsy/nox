"""Nightly forgetting job: purges expired `memory_items` and `vault_note_versions` rows, every
deletion audited (Data Model "Retention" - "deletion is audited"). Turn retention is
`nox.data.repos.TurnRepository.purge_expired`, already shipped in v0.1 and out of this job's scope;
this job only owns the memory-item side adds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import MemoryItemRepository
from nox.memory.embeddings import EmbeddingService

log = get_logger(__name__)


class AuditSink(Protocol):
    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RetentionReport:
    memory_items_purged: int = 0
    note_versions_purged: int = 0


class RetentionJob:
    """`await run` once, e.g. from a nightly scheduler (maintenance window is
    concern; this job is idempotent and cheap enough to also run at boot)."""

    def __init__(
        self,
        repo: MemoryItemRepository,
        db: Database,
        *,
        embeddings: EmbeddingService | None = None,
        audit: AuditSink | None = None,
        bus: EventBus | None = None,
        note_version_retention_days: int = 30,
    ) -> None:
        self._repo = repo
        self._db = db
        self._embeddings = embeddings
        self._audit = audit
        self._bus = bus
        self._note_version_retention_days = note_version_retention_days

    async def run(self, now: datetime | None = None) -> RetentionReport:
        now = now or datetime.now(UTC)
        expired = self._repo.list_expired(now)
        for item in expired:
            self._repo.delete(item.id)
            if self._embeddings is not None:
                self._embeddings.remove("memory_item", item.id)
            self._audit_delete(str(item.id), "memory_item")
            if self._bus is not None:
                await self._bus.publish(
                    Event(
                        name=E.MEMORY_DELETED,
                        payload={"id": item.id, "reason": "retention"},
                        source="memory",
                    )
                )

        cutoff = (now - timedelta(days=self._note_version_retention_days)).isoformat()
        cur = self._db.execute("DELETE FROM vault_note_versions WHERE saved_at < ?", (cutoff,))
        versions_purged = int(cur.rowcount)
        if versions_purged:
            self._audit_delete(f"{versions_purged} rows", "vault_note_versions")

        return RetentionReport(
            memory_items_purged=len(expired), note_versions_purged=versions_purged
        )

    def _audit_delete(self, target: str, table: str) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor="system",
                tool="memory",
                action="retention.purge",
                target=target,
                decision="allow",
                result="ok",
                details={"table": table},
            )
        except Exception as exc:  # noqa: BLE001 - audit failure must not stop retention
            log.error("memory.retention_audit_failed", error=str(exc))
