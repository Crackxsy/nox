"""`install(core) -> MemoryRuntime`: wires memory and the Obsidian vault onto a started core.

Wires, in this order: `EmbeddingService` (Ollama `nomic-embed-text` into sqlite-vec, FTS5 fallback)
-> `VaultIndexer` + `VaultWatcher` (initial full scan, then live watch) -> `MemoryService` /
`VaultWriter` (privacy- and zone-gated) -> `RetrievalService` -> the four memory/vault tools -> an
adapter that makes the orchestrator's system prompt retrieval-augmented.

Embeddings are only ever requested through the security layer's egress guard. Without a guard the
embedding provider is not built at all and the `memory.embeddings` health check reports
`unavailable` with the reason - notes stay searchable through FTS5, but no text leaves the process
unguarded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from nox.ai.config import OllamaConfig
from nox.ai.providers.ollama import OllamaProvider
from nox.core.events import E, Event, HealthStatus
from nox.core.health import Check
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

#: Marker set on an orchestrator whose prompt this module already wrapped, so a second install
#: cannot stack two adapters on top of each other.
_ADAPTER_MARKER = "_nox_memory_prompt_adapter"


class _Core(Protocol):
    """The subset of the core this module needs, declared locally rather than importing the
    composition root - `nox.memory` must not depend on `nox.app`."""

    config: Any
    db: Any
    bus: Any
    security: Any
    tool_registry: Any
    orchestrator: Any
    health: Any


class _PromptAdapter:
    """Appends the memory context retrieved for the current turn to the system prompt.

    The orchestrator's `system_prompt` is a synchronous callable while retrieval is async, so the
    context is refreshed just before a turn starts and cached for the synchronous call that
    follows immediately after.

    This adapter replaces two attributes on the live orchestrator object, because the orchestrator
    exposes no registration seam yet. The seam it needs is one method - register an async
    `Callable[[str], Awaitable[str]]` that the orchestrator awaits itself before building the
    system message - at which point `install` registers through it and this class shrinks to
    that one function. Until then, everything replaced here is restored by `uninstall`.
    """

    def __init__(
        self, orchestrator: Any, retrieval: RetrievalService, *, k: int, timeout_s: float = 2.0
    ) -> None:
        self._orchestrator = orchestrator
        self._base_prompt: Callable[[], str] = orchestrator.system_prompt
        self._base_handle_text = orchestrator.handle_text
        self._retrieval = retrieval
        self._k = k
        self._timeout_s = timeout_s
        self._context_block = ""
        self.last_result: RetrievalResult | None = None

    def install(self) -> None:
        if getattr(self._orchestrator, _ADAPTER_MARKER, None) is not None:
            raise RuntimeError("the orchestrator prompt is already memory-augmented")
        self._orchestrator.system_prompt = self
        self._orchestrator.handle_text = self._handle_text
        setattr(self._orchestrator, _ADAPTER_MARKER, self)

    def uninstall(self) -> None:
        if getattr(self._orchestrator, _ADAPTER_MARKER, None) is not self:
            return
        self._orchestrator.system_prompt = self._base_prompt
        self._orchestrator.handle_text = self._base_handle_text
        setattr(self._orchestrator, _ADAPTER_MARKER, None)

    async def _handle_text(self, text: str, **kwargs: Any) -> Any:
        await self.refresh(text)
        return await self._base_handle_text(text, **kwargs)

    async def refresh(self, query: str) -> None:
        try:
            result = await asyncio.wait_for(
                self._retrieval.retrieve(query, k=self._k), timeout=self._timeout_s
            )
        except TimeoutError:
            log.warning("memory.retrieval_timeout", timeout_s=self._timeout_s)
            return
        except Exception as exc:  # noqa: BLE001 - a broken retrieval must not break the turn
            log.warning("memory.retrieval_failed", error=f"{type(exc).__name__}: {exc}")
            return
        self.last_result = result
        self._context_block = format_context(result)

    def __call__(self) -> str:
        base = self._base_prompt()
        if not self._context_block:
            return base
        return f"{base}\n\n## Memory context (retrieved)\n{self._context_block}"


@dataclass
class MemoryRuntime:
    """Handles for the caller. `install` never blocks on the watcher or the first full scan."""

    embeddings: EmbeddingService
    indexer: VaultIndexer
    watcher: VaultWatcher
    writer: VaultWriter
    memory: MemoryService
    retrieval: RetrievalService
    retention: RetentionJob
    #: Empty while embeddings work; otherwise why they do not, for the health check and the user.
    embeddings_unavailable: str = ""
    scan_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    scan_error: str = ""
    prompt_adapter: _PromptAdapter | None = field(default=None, repr=False)

    async def stop(self) -> None:
        self.watcher.stop()
        if self.scan_task is not None and not self.scan_task.done():
            self.scan_task.cancel()
            await asyncio.gather(self.scan_task, return_exceptions=True)
        if self.prompt_adapter is not None:
            self.prompt_adapter.uninstall()
            self.prompt_adapter = None

    async def health(self) -> tuple[HealthStatus, str]:
        if self.embeddings_unavailable:
            return HealthStatus.UNAVAILABLE, self.embeddings_unavailable
        if self.scan_error:
            return HealthStatus.LIMITED, f"the first vault scan failed: {self.scan_error}"
        return HealthStatus.AVAILABLE, "semantic search available"


def _build_embed_provider(core: _Core, embed_model: str) -> tuple[OllamaProvider | None, str]:
    """Return `(provider, unavailable_reason)`.

    The provider is built only when Ollama is enabled *and* the egress guard is present: an
    embedding request that bypassed the guard would be an unpoliced outbound request, so a missing
    guard disables embeddings rather than relaxing them.
    """
    ollama_cfg = getattr(core.config.ai.providers, "ollama", None)
    if ollama_cfg is not None and not ollama_cfg.enabled:
        return None, ""
    egress = getattr(core.security, "egress", None)
    if egress is None:
        reason = "no egress guard on the security layer, so embeddings stay disabled"
        log.error("memory.embed_provider_unguarded", reason=reason)
        return None, reason
    base_url = ollama_cfg.base_url if ollama_cfg is not None else "http://127.0.0.1:11434"

    def factory() -> httpx.AsyncClient:
        # Same wiring as the chat provider: every request goes through the guard and reuses its
        # shared SSL context instead of building one per chunk.
        client: httpx.AsyncClient = egress.client(timeout=httpx.Timeout(30.0, connect=5.0))
        return client

    return OllamaProvider(OllamaConfig(base_url=base_url, embed_model=embed_model), factory), ""


def install(core: _Core) -> MemoryRuntime:
    """Wire memory and the vault onto `core` and return the handles. Returns immediately; the
    first full vault scan runs in the background."""
    cfg = core.config.memory
    vault_dir = core.config.paths.vault_dir
    privacy = core.security.privacy

    provider, embeddings_unavailable = _build_embed_provider(core, cfg.embed_model)
    embeddings = EmbeddingService(core.db, provider, model=cfg.embed_model)
    memory_repo = MemoryItemRepository(core.db)
    retrieval = RetrievalService(core.db, embeddings, max_tokens=cfg.retrieval_max_tokens)

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
    runtime = MemoryRuntime(
        embeddings=embeddings,
        indexer=indexer,
        watcher=VaultWatcher(indexer, vault_dir, debounce_s=cfg.vault_watch_debounce_s),
        writer=VaultWriter(core.db, vault_dir, zones=privacy),
        memory=MemoryService(memory_repo, privacy, embeddings=embeddings, bus=core.bus),
        retrieval=retrieval,
        retention=RetentionJob(
            memory_repo,
            core.db,
            embeddings=embeddings,
            audit=core.security.audit,
            bus=core.bus,
            note_version_retention_days=cfg.retention_note_version_days,
        ),
        embeddings_unavailable=embeddings_unavailable,
    )

    register_memory_tools(
        core.tool_registry,
        retrieval=retrieval,
        memory=runtime.memory,
        writer=runtime.writer,
        vault_dir=vault_dir,
        zones=privacy,
        indexer=indexer,
    )

    adapter = _PromptAdapter(core.orchestrator, retrieval, k=cfg.retrieval_k)
    adapter.install()
    runtime.prompt_adapter = adapter

    if cfg.full_scan_on_boot:
        runtime.scan_task = asyncio.get_running_loop().create_task(
            indexer.full_scan(), name="nox-memory-full-scan"
        )
        runtime.scan_task.add_done_callback(lambda task: _note_scan_result(runtime, task))
    runtime.watcher.start()

    async def _on_stopping(_event: Event) -> None:
        runtime.watcher.stop()

    core.bus.subscribe(E.SYSTEM_STOPPING, _on_stopping)
    _register_health_check(core, runtime)
    return runtime


def _note_scan_result(runtime: MemoryRuntime, task: asyncio.Task[Any]) -> None:
    """A failed first scan leaves the vault index empty; say so instead of dropping the error."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    runtime.scan_error = f"{type(exc).__name__}: {exc}"
    log.error("memory.full_scan_failed", error=runtime.scan_error)


def _register_health_check(core: _Core, runtime: MemoryRuntime) -> None:
    health = getattr(core, "health", None)
    if health is None:
        return
    try:
        health.add_check(Check("memory.embeddings", runtime.health))
    except ValueError:  # installed twice on the same core (tests)
        log.debug("memory.health_check_already_registered")


__all__ = ["MemoryRuntime", "install"]
