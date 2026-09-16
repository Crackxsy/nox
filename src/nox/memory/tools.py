"""Memory & vault tool catalogue (ST-07 tools): `memory.search` (read), `memory.write` (medium,
profile/privacy gated), `vault.read` (read, zones enforced), `vault.append_inbox` (medium)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.memory.importance import MemoryType
from nox.memory.items import MemoryService, MemoryWriteRefusedError
from nox.memory.retrieval import RetrievalService
from nox.memory.vault_index import VaultIndexer
from nox.memory.vault_writer import VaultWriter, VaultWriteRefusedError
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec


class ZoneMatcher(Protocol):
    def path_zone(self, path: str) -> str | None: ...


# ---- memory.search ----------------------------------------------------------------------------


class MemorySearchInput(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(default=8, ge=1, le=50)


def make_memory_search_tool(retrieval: RetrievalService) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = MemorySearchInput.model_validate(arguments)
        result = await retrieval.retrieve(parsed.query, k=parsed.k)
        return {
            "found": result.found,
            "limited": result.limited,
            "latency_ms": round(result.latency_ms, 1),
            "items": [
                {
                    "kind": item.kind,
                    "ref_id": item.ref_id,
                    "score": item.score,
                    "text": item.text,
                    "source": item.source,
                    "via": item.via,
                }
                for item in result.items
            ],
        }

    return ToolSpec(
        name="memory.search",
        description="Search Nox's memory items and indexed vault chunks for a query.",
        input_model=MemorySearchInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("query") or ""),
    )


# ---- memory.write ------------------------------------------------------------------------------


class MemoryWriteInput(BaseModel):
    text: str = Field(min_length=1)
    type: str = MemoryType.CONVERSATION.value
    source: str = "tool"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    explicit: bool | None = None


def make_memory_write_tool(memory: MemoryService) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = MemoryWriteInput.model_validate(arguments)
        try:
            row = await memory.create(
                parsed.text,
                type=parsed.type,
                source=parsed.source,
                explicit=parsed.explicit,
            )
        except MemoryWriteRefusedError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "id": row.id, "importance": row.importance, "type": row.type}

    return ToolSpec(
        name="memory.write",
        description="Write a new memory item (importance scored, privacy-gated).",
        input_model=MemoryWriteInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("type") or ""),
    )


# ---- vault.read ---------------------------------------------------------------------------------


class VaultReadInput(BaseModel):
    path: str = Field(min_length=1)


def make_vault_read_tool(vault_dir: Path, zones: ZoneMatcher) -> ToolSpec:
    vault_dir = Path(vault_dir)

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = VaultReadInput.model_validate(arguments)
        resolved = _resolve_within(vault_dir, parsed.path)
        if resolved is None:
            return {"ok": False, "reason": "path escapes the vault"}
        if zones.path_zone(str(resolved)) is not None or zones.path_zone(parsed.path) is not None:
            return {"ok": False, "reason": "privacy zone"}
        if not resolved.is_file():
            return {"ok": False, "reason": "not found"}
        return {"ok": True, "path": parsed.path, "text": resolved.read_text(encoding="utf-8")}

    return ToolSpec(
        name="vault.read",
        description="Read one vault note by path (privacy zones enforced).",
        input_model=VaultReadInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("path") or ""),
    )


# ---- vault.append_inbox -------------------------------------------------------------------------


class VaultAppendInboxInput(BaseModel):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    source: str = "tool"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def make_vault_append_inbox_tool(
    writer: VaultWriter, indexer: VaultIndexer | None = None
) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = VaultAppendInboxInput.model_validate(arguments)
        try:
            result = writer.write_inbox_note(
                parsed.title, parsed.body, source=parsed.source, confidence=parsed.confidence
            )
        except VaultWriteRefusedError as exc:
            return {"ok": False, "reason": str(exc)}
        if indexer is not None:
            await indexer.index_path(result.path)
        return {"ok": True, "path": str(result.path), "action": result.action}

    return ToolSpec(
        name="vault.append_inbox",
        description="Write a new Nox-authored note to 00 - Inbox with provenance frontmatter.",
        input_model=VaultAppendInboxInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("title") or ""),
    )


def register_memory_tools(
    registry: ToolRegistry,
    *,
    retrieval: RetrievalService,
    memory: MemoryService,
    writer: VaultWriter,
    vault_dir: Path,
    zones: ZoneMatcher,
    indexer: VaultIndexer | None = None,
) -> None:
    registry.register(make_memory_search_tool(retrieval))
    registry.register(make_memory_write_tool(memory))
    registry.register(make_vault_read_tool(vault_dir, zones))
    registry.register(make_vault_append_inbox_tool(writer, indexer))


def _resolve_within(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate
