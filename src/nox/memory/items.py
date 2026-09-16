"""MemoryService: the write-refusal boundary for `memory_items` (ST-07-01).

Wraps `MemoryItemRepository` with importance scoring, embedding writes, and `memory.created`/
`memory.deleted` events. A write is refused - not silently downgraded - whenever
`PrivacyGate.allows_memory_write()` is False (PRIVATE mode, an active privacy zone, or safe mode).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.repos import MemoryItemRepository, MemoryItemRow
from nox.memory.embeddings import EmbeddingService
from nox.memory.importance import MemoryType, score_importance

log = get_logger(__name__)


class PrivacyGate(Protocol):
    def allows_memory_write(self) -> bool: ...


class MemoryWriteRefusedError(RuntimeError):
    """`PrivacyGate.allows_memory_write()` was False when `MemoryService.create()` was called."""


@dataclass
class MemoryService:
    repo: MemoryItemRepository
    privacy: PrivacyGate
    embeddings: EmbeddingService | None = None
    bus: EventBus | None = None

    async def create(
        self,
        text: str,
        *,
        type: MemoryType | str = MemoryType.CONVERSATION,  # noqa: A002
        source: str = "",
        explicit: bool | None = None,
        privacy_class: str = "normal",
        vault_path: str | None = None,
    ) -> MemoryItemRow:
        if not self.privacy.allows_memory_write():
            raise MemoryWriteRefusedError(
                "memory write refused: privacy mode/zone/safe-mode forbids it"
            )
        type_value = type.value if isinstance(type, MemoryType) else str(type)
        importance = score_importance(text, explicit=explicit)
        row = self.repo.add(
            type=type_value,
            text=text,
            importance=importance,
            source=source,
            vault_path=vault_path,
            privacy_class=privacy_class,
        )
        if self.embeddings is not None:
            await self.embeddings.embed_and_store("memory_item", row.id, text)
        await self._publish(
            E.MEMORY_CREATED,
            {
                "id": row.id,
                "type": row.type,
                "importance": row.importance,
                "source": row.source,
                "vault_path": row.vault_path,
            },
        )
        return row

    async def delete(self, item_id: int, *, reason: str = "") -> bool:
        row = self.repo.get(item_id)
        if row is None:
            return False
        ok = self.repo.delete(item_id)
        if not ok:
            return False
        if self.embeddings is not None:
            self.embeddings.remove("memory_item", item_id)
        await self._publish(E.MEMORY_DELETED, {"id": item_id, "reason": reason})
        return True

    async def search(self, query: str, *, k: int = 10) -> list[MemoryItemRow]:
        if self.embeddings is None:
            return self.repo.search_fts(query, k)
        hits = await self.embeddings.search(query, k=k, kind="memory_item")
        rows = self.repo.list_by_ids([h.ref_id for h in hits])
        order = {h.ref_id: i for i, h in enumerate(hits)}
        rows.sort(key=lambda r: order.get(r.id, len(order)))
        return rows

    async def _publish(self, name: str, payload: dict[str, object]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(Event(name=name, payload=payload, source="memory"))
        except Exception as exc:  # noqa: BLE001 - an event-bus failure must not break the write
            log.warning("memory.publish_failed", event_name=name, error=str(exc))
