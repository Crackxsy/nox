"""`install(core) -> None`: wires EPIC-07 Memory & Vault into an already-booted `NoxCore` without
touching `src/nox/app.py` (ENGINEERING.md shared-file rule). Call this once, after `core.start()`.

Wires: `EmbeddingService` (Ollama `nomic-embed-text` + sqlite-vec, FTS5 fallback) -> `VaultIndexer`
+ `VaultWatcher` (initial full scan, then live watch) -> `MemoryService`/`VaultWriter` (privacy/zone
gated) -> `RetrievalService` -> the four memory/vault tools -> a small adapter that makes
`core.orchestrator.system_prompt` retrieval-augmented.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from nox.ai.config import OllamaConfig
from nox.ai.providers.ollama import OllamaProvider
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.data.repos import MemoryItemRepository
from nox.memory.embeddings import EmbeddingService
from nox.memory.items import MemoryService
from nox.memory.retention import RetentionJob
from nox.memory.retrieval import RetrievalResult, RetrievalService, format_context
from nox.memory.tools import register_memory_tools
from nox.memory.vault_index import VaultIndexer
from nox.memory.vault_watcher import VaultWatcher
from nox.memory.vault_writer import VaultWriter

log = get_logger(__name__)


class _Core(Protocol):
    """The subset of `NoxCore` this module needs (kept narrow and local rather than importing
    `nox.app.NoxCore`, which would make `nox.memory` depend on the composition root)."""

    config: Any
    db: Any
    bus: Any
    security: Any
    tool_registry: Any
    orchestrator: Any


@dataclass
class MemoryRuntime:
    """Handles returned to the caller (tests, `core.stop()` wiring) - `install()` itself never
    blocks on the watcher/full scan finishing."""

    embeddings: EmbeddingService
    indexer: VaultIndexer
    watcher: VaultWatcher
    writer: VaultWriter
    memory: MemoryService
    retrieval: RetrievalService
    retention: RetentionJob


class _PromptAdapter:
    """Replaces `Orchestrator.system_prompt` (a sync `Callable[[], str]`, ST-05/Component Model)
    with one that appends the most recently retrieved memory context. Retrieval is async and the
    contract is not, so `refresh(query)` is run just before each turn (see the `handle_text` wrap
    below) and the result is cached for the synchronous call that follows immediately after."""

    def __init__(
        self, base_prompt: Callable[[], str], retrieval: RetrievalService, *, k: int
    ) -> None:
        self._base_prompt = base_prompt
        self._retrieval = retrieval
        self._k = k
        self._context_block = ""
        self.last_result: RetrievalResult | None = None

    async def refresh(self, query: str) -> None:
        try:
            result = await self._retrieval.retrieve(query, k=self._k)
        except Exception as exc:  # noqa: BLE001 - a broken retrieval must not break the turn
            log.warning("memory.retrieval_failed", error=str(exc))
            return
        self.last_result = result
        self._context_block = format_context(result)

    def __call__(self) -> str:
        base = self._base_prompt()
        if not self._context_block:
            return base
        return f"{base}\n\n## Memory context (retrieved)\n{self._context_block}"


def install(core: _Core) -> None:
    cfg = core.config.memory
    vault_dir = core.config.paths.vault_dir
    privacy = core.security.privacy

    provider: OllamaProvider | None = None
    ollama_cfg = getattr(core.config.ai.providers, "ollama", None)
    if ollama_cfg is None or ollama_cfg.enabled:
        base_url = ollama_cfg.base_url if ollama_cfg is not None else "http://127.0.0.1:11434"
        # Same wiring as the chat provider in `NoxCore`: every request goes through the egress
        # guard (B-11) and reuses its shared SSL context instead of building one per chunk.
        egress = getattr(core.security, "egress", None)
        factory = (
            (lambda: egress.client(timeout=httpx.Timeout(30.0, connect=5.0)))
            if egress is not None
            else None
        )
        if egress is None:
            log.warning("memory.embed_provider_unguarded", note="no egress guard on core.security")
        provider = OllamaProvider(
            OllamaConfig(base_url=base_url, embed_model=cfg.embed_model), factory
        )

    embeddings = EmbeddingService(core.db, provider, model=cfg.embed_model)
    memory_repo = MemoryItemRepository(core.db)
    memory = MemoryService(memory_repo, privacy, embeddings=embeddings, bus=core.bus)
    writer = VaultWriter(core.db, vault_dir, zones=privacy)
    retrieval = RetrievalService(core.db, embeddings, max_tokens=cfg.retrieval_max_tokens)
    retention = RetentionJob(
        memory_repo,
        core.db,
        embeddings=embeddings,
        audit=core.security.audit,
        bus=core.bus,
        note_version_retention_days=cfg.retention_note_version_days,
    )

    async def on_index_updated(path: str, chunk_count: int) -> None:
        await core.bus.publish(
            Event(
                name=E.MEMORY_INDEX_UPDATED,
                payload={"path": path, "chunks": chunk_count},
                source="memory",
            )
        )

    indexer = VaultIndexer(
        core.db, vault_dir, zones=privacy, embeddings=embeddings, on_index_updated=on_index_updated
    )
    watcher = VaultWatcher(indexer, vault_dir, debounce_s=cfg.vault_watch_debounce_s)

    register_memory_tools(
        core.tool_registry,
        retrieval=retrieval,
        memory=memory,
        writer=writer,
        vault_dir=vault_dir,
        zones=privacy,
        indexer=indexer,
    )

    _install_prompt_adapter(core, retrieval, k=cfg.retrieval_k)

    loop = asyncio.get_running_loop()
    if cfg.full_scan_on_boot:
        loop.create_task(indexer.full_scan(), name="nox-memory-full-scan")
    watcher.start()

    async def _on_stopping(_event: Event) -> None:
        watcher.stop()

    core.bus.subscribe(E.SYSTEM_STOPPING, _on_stopping)

    # Not part of the `_Core` Protocol (kept narrow for typing); a plain attribute for tests and
    # any later service that needs direct access, e.g. a maintenance-window job calling
    # `core.memory.retention.run()`.
    core.memory = MemoryRuntime(  # type: ignore[attr-defined]
        embeddings=embeddings,
        indexer=indexer,
        watcher=watcher,
        writer=writer,
        memory=memory,
        retrieval=retrieval,
        retention=retention,
    )


def _install_prompt_adapter(core: _Core, retrieval: RetrievalService, *, k: int) -> None:
    orchestrator = core.orchestrator
    adapter = _PromptAdapter(orchestrator.system_prompt, retrieval, k=k)
    original_handle_text = orchestrator.handle_text

    async def handle_text_with_memory(text: str, **kwargs: Any) -> Any:
        await adapter.refresh(text)
        return await original_handle_text(text, **kwargs)

    orchestrator.system_prompt = adapter
    orchestrator.handle_text = handle_text_with_memory
