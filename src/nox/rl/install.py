"""Composition entry point for the Rocket League companion's core-side services.

`install(core)` wires the mode bridge, database persistence, the callout engine, the post-match
announcer, session-end summaries and the replay backfill task onto a started core, and returns the
runtime that stops them again. The `rl` plugin itself stays opt-in; these services are harmless no-
ops while it is not running, because they only react to events it emits.

Vision is installed on top when its dependencies are present. A vision that cannot be installed is
recorded on the runtime and reported by the `rl.vision` health check as `unavailable` with the
reason - it never silently disappears, and it never takes the rest of the extension down with it.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.core.tasks import TaskQueue
from nox.data.repos import TaskRepository, TaskRow
from nox.data.rl_repos import RlEventRepository, RlMatchRepository, RlReplayRepository
from nox.rl import replay_parser
from nox.rl.paths import default_replay_folder
from nox.rl.services import (
    CalloutEngineProtocol,
    RlCalloutService,
    RlMatchAnnouncer,
    RlModeBridge,
    RlPersistenceService,
    RlSessionSummaryService,
)

log = get_logger(__name__)

_BACKFILL_KIND = "rl_replay_backfill"
_BACKFILL_TASK_ID = "rl-replay-backfill"


@dataclass
class RlRuntime:
    """Handles for the caller. `stop` shuts the backfill queue down before the database closes -
    it used to keep writing into a connection that was already gone."""

    mode_bridge: RlModeBridge
    persistence: RlPersistenceService
    callouts: RlCalloutService
    announcer: RlMatchAnnouncer
    summaries: RlSessionSummaryService
    backfill_queue: TaskQueue | None = None
    #: Empty when vision is running; otherwise why it is not.
    vision_unavailable: str = ""
    vision: Any = field(default=None, repr=False)

    async def stop(self) -> None:
        if self.backfill_queue is not None:
            await self.backfill_queue.stop()
        if self.vision is not None:
            await self.vision.stop()
        await self.summaries.stop()
        await self.announcer.stop()
        await self.callouts.stop()
        await self.persistence.stop()
        await self.mode_bridge.stop()

    async def vision_health(self) -> tuple[HealthStatus, str]:
        if self.vision_unavailable:
            return HealthStatus.UNAVAILABLE, self.vision_unavailable
        return HealthStatus.AVAILABLE, "HUD frame analysis available"


class _NullCalloutEngine:
    """Stands in when the plugin's rule engine cannot be imported: no callout ever fires."""

    def evaluate(self, **_kwargs: Any) -> None:
        return None


def install(core: Any) -> RlRuntime:
    """`core` is a started core, or a test double exposing the same `config`, `db`, `bus`,
    `state`, `security`, `speaker` and `router` attributes."""
    cfg = core.config.rl
    matches = RlMatchRepository(core.db)
    replays = RlReplayRepository(core.db)

    runtime = RlRuntime(
        mode_bridge=RlModeBridge(core.bus, core.state, core.security.engine),
        persistence=RlPersistenceService(
            core.bus,
            matches,
            RlEventRepository(core.db),
            replays,
            events_retain_days=cfg.retention.events_days,
            matches_retain_days=cfg.retention.matches_days,
        ),
        callouts=RlCalloutService(core.bus, _build_callout_engine(cfg), core.speaker),
        announcer=RlMatchAnnouncer(core.bus, core.speaker),
        summaries=RlSessionSummaryService(core.bus, matches, router=core.router, vault_writer=None),
    )
    runtime.mode_bridge.start()
    runtime.persistence.start()
    runtime.callouts.start()
    runtime.announcer.start()
    runtime.summaries.start()

    runtime.backfill_queue = _build_backfill_queue(core, cfg, replays)
    _install_vision(core, runtime)
    return runtime


def _build_callout_engine(cfg: Any) -> CalloutEngineProtocol:
    """The rule engine lives in the plugin package, which the core does not depend on; without it
    callouts are honestly absent rather than silently wrong."""
    try:
        plugin_src = Path(__file__).resolve().parents[3] / "plugins" / "rl" / "src"
        if str(plugin_src) not in sys.path and plugin_src.is_dir():
            sys.path.append(str(plugin_src))
        from nox_plugin_rl.callouts import CalloutEngine
    except ImportError:
        log.warning("rl.callout_engine_unavailable")
        return _NullCalloutEngine()
    engine: CalloutEngineProtocol = CalloutEngine(
        min_confidence=cfg.callouts.min_confidence,
        cooldown_s=cfg.callouts.cooldown_s,
        min_per_minute=cfg.callouts.min_per_minute,
        max_per_minute=cfg.callouts.max_per_minute,
    )
    return engine


def _install_vision(core: Any, runtime: RlRuntime) -> None:
    """Vision is additive: a failure here degrades vision alone, never the rest of the extension.

    The failure is recorded and reported by a health check instead of leaving the feature quietly
    missing with nothing but a log line behind it.
    """
    try:
        from nox.rl.vision import install_vision

        runtime.vision = install_vision(core)
    except Exception as exc:  # noqa: BLE001 - reported through the health check below
        runtime.vision_unavailable = f"{type(exc).__name__}: {exc}"
        log.warning("rl.vision_install_failed", error=runtime.vision_unavailable, exc_info=True)
    try:
        core.health.add_check(Check("rl.vision", runtime.vision_health))
    except ValueError:  # installed twice on the same core (tests)
        log.debug("rl.vision_health_check_already_registered")


def _build_backfill_queue(core: Any, cfg: Any, replays: RlReplayRepository) -> TaskQueue:
    """Backlog parse of the whole replay folder through a dedicated `TaskQueue`.

    A dedicated queue - the core runs none by default - is what makes the backfill pause on its
    own while `rocket_league` mode is active, so parsing never competes with the game.
    """
    task_repo = TaskRepository(core.db)
    queue = TaskQueue(core.bus, task_repo)
    folder = Path(cfg.replay.folder) if cfg.replay.folder else default_replay_folder()
    batch_size = cfg.replay.backfill_batch_size

    async def _handler(row: TaskRow, checkpoint: Any) -> None:
        if not folder.is_dir():
            return
        files = sorted(folder.glob("*.replay"))
        start = int((row.checkpoint or {}).get("index", 0))
        for i in range(start, len(files)):
            path = files[i]
            if replays.get_by_path(str(path)) is not None:
                continue
            # Parsing is CPU-bound and synchronous; a backlog of thousands of replays used to run
            # entirely on the event loop and blocked the core for over a minute at boot. Parse
            # off-loop and yield between files.
            parsed = await asyncio.to_thread(replay_parser.parse_replay_file, str(path))
            summary = parsed.summary() if parsed.parse_status != "failed" else {}
            replays.upsert(
                file_path=str(path),
                file_hash="",
                parser_version=replay_parser.PARSER_VERSION,
                parse_status=parsed.parse_status,
                header=summary,
            )
            if (i - start) % batch_size == 0:
                checkpoint({"index": i + 1})
            await asyncio.sleep(0)
        checkpoint({"index": len(files)})

    queue.register(_BACKFILL_KIND, _handler)
    if task_repo.get(_BACKFILL_TASK_ID) is None:
        task_repo.add(_BACKFILL_KIND, {}, priority=-10, task_id=_BACKFILL_TASK_ID)
    queue.start()
    return queue


__all__ = ["RlRuntime", "install"]
