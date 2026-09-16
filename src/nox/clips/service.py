"""ClipCaptureService (ST-15-02/03, Spec v0.6 §4.1/§4.2): the core-side subscriber to
`clip.requested`. The `clips` plugin never talks to OBS itself (no cross-plugin tool call in the
Plugin API) - this service is the one that calls `obs.replay_buffer.save` through `ToolExecutor`,
copies the resolved file into the clip library, indexes it (`clips` row, `status="new"`) and
reports the outcome as `clip.saved`/`clip.failed`. A second, defensive cooldown runs here too (the
detector already gates per `trigger_kind`, but this is the point that actually spends OBS calls)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from nox.clips.ingest import ingest_file
from nox.clips.repository import ClipRepository
from nox.core.config import ClipsConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

Unsubscribe = Callable[[], None]
AGENT = "nox.clips"


class ClipCaptureService:
    def __init__(
        self,
        bus: EventBus,
        executor: ToolExecutor,
        repo: ClipRepository,
        config: ClipsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._executor = executor
        self._repo = repo
        self._config = config
        self._clock = clock
        self._last_call: dict[str, float] = {}
        self._unsubs: list[Unsubscribe] = []

    def start(self) -> None:
        self._unsubs = [self._bus.subscribe(E.CLIP_REQUESTED, self._on_requested)]

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    def _on_cooldown(self, trigger_kind: str) -> bool:
        last = self._last_call.get(trigger_kind)
        return last is not None and (self._clock() - last) < self._config.cooldown_s

    async def _fail(self, trigger_kind: str, source: str, reason: str) -> None:
        log.info("clips.capture_failed", trigger_kind=trigger_kind, reason=reason)
        await self._bus.publish(
            Event(
                name=E.CLIP_FAILED,
                payload={"trigger_kind": trigger_kind, "source": source, "reason": reason},
            )
        )

    async def _on_requested(self, ev: Event) -> None:
        trigger_kind = str(ev.payload.get("trigger_kind", ""))
        source = str(ev.payload.get("source", "event"))
        session_id = str(ev.payload.get("session_id", ""))
        origin_event_id = str(ev.payload.get("origin_event_id", ""))
        tags = [str(t) for t in ev.payload.get("tags", [])]

        if self._on_cooldown(trigger_kind):
            log.info("clips.capture_cooldown_skipped", trigger_kind=trigger_kind)
            return
        self._last_call[trigger_kind] = self._clock()

        result = await self._executor.call(
            agent=AGENT,
            name="obs.replay_buffer.save",
            arguments={"reason": trigger_kind},
            mode="stream",
        )
        if not result.ok or not result.data:
            await self._fail(trigger_kind, source, result.error or "obs.replay_buffer.save failed")
            return
        if not result.data.get("ok", False):
            await self._fail(
                trigger_kind, source, str(result.data.get("reason") or "replay buffer save failed")
            )
            return
        file_path = result.data.get("file_path")
        if not file_path:
            await self._fail(trigger_kind, source, "no file path returned by OBS")
            return

        src = Path(str(file_path))
        try:
            ingested = await asyncio.to_thread(
                ingest_file, src, library_root=self._config.library_root
            )
        except OSError as exc:
            await self._fail(trigger_kind, source, f"could not ingest {src}: {exc}")
            return

        existing = self._repo.get_by_checksum(ingested.checksum)
        if existing is not None:
            # Same bytes already indexed (e.g. a watcher pass beat us to it) - report the existing
            # clip rather than creating a duplicate row.
            row = existing
        else:
            row = self._repo.insert(
                source=source,
                trigger_kind=trigger_kind,
                file_path=str(ingested.file_path),
                origin_event_id=origin_event_id,
                session_id=session_id,
                duration_s=ingested.duration_s,
                thumbnail_path=str(ingested.thumbnail_path) if ingested.thumbnail_path else None,
                tags=tags,
                checksum=ingested.checksum,
            )
        await self._bus.publish(
            Event(
                name=E.CLIP_SAVED,
                payload={
                    "clip_id": row.id,
                    "file_path": row.file_path,
                    "trigger_kind": trigger_kind,
                    "source": source,
                    "duration_s": row.duration_s,
                    "tags": row.tags,
                },
            )
        )
