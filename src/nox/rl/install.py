"""Composition entry point for Rocket League Stage 1's core-side slice (Spec v0.3 EPIC-12,
ST-12-01..08): `install(core)` wires the mode bridge, DB persistence, callout engine, post-match
announcer, session-end summaries and the replay backfill task onto a started `NoxCore`. Mirrors
`nox.pm.install`'s pattern - deliberately not wired into `src/nox/app.py` here (shared file, out of
this story's scope); the integrator calls `nox.rl.install.install(core)` after `core.start()`.

The `rl` plugin itself stays opt-in (`plugins.enabled`, `config/defaults.yaml`) - this module's
services are harmless no-ops when the plugin is not running (they only react to events it emits).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

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


def install(core: Any) -> RlRuntime:
    """`core` is a started `nox.app.NoxCore` (or a test double exposing the same `config`, `db`,
    `bus`, `state`, `security`, `speaker` attributes)."""
    cfg = core.config.rl
    matches = RlMatchRepository(core.db)
    events = RlEventRepository(core.db)
    replays = RlReplayRepository(core.db)

    mode_bridge = RlModeBridge(core.bus, core.state, core.security.engine)
    mode_bridge.start()

    persistence = RlPersistenceService(
        core.bus,
        matches,
        events,
        replays,
        events_retain_days=cfg.retention.events_days,
        matches_retain_days=cfg.retention.matches_days,
    )
    persistence.start()

    engine: CalloutEngineProtocol
    try:
        _plugin_src = Path(__file__).resolve().parents[3] / "plugins" / "rl" / "src"
        if str(_plugin_src) not in sys.path and _plugin_src.is_dir():
            sys.path.insert(0, str(_plugin_src))
        from nox_plugin_rl.callouts import CalloutEngine

        engine = CalloutEngine(
            min_confidence=cfg.callouts.min_confidence,
            cooldown_s=cfg.callouts.cooldown_s,
            min_per_minute=cfg.callouts.min_per_minute,
            max_per_minute=cfg.callouts.max_per_minute,
        )
    except ImportError:
        log.warning("rl.callout_engine_unavailable")
        engine = _NullCalloutEngine()

    speaker = getattr(core, "speaker", None)
    callouts = RlCalloutService(core.bus, engine, speaker)
    callouts.start()

    announcer = RlMatchAnnouncer(core.bus, speaker)
    announcer.start()

    router = getattr(core, "router", None)
    summaries = RlSessionSummaryService(core.bus, matches, router=router, vault_writer=None)
    summaries.start()

    core.rl_mode_bridge = mode_bridge
    core.rl_persistence = persistence
    core.rl_callouts = callouts
    core.rl_announcer = announcer
    core.rl_summaries = summaries

    _install_backfill_task(core, cfg, replays)

    # Vision Stage 2 (Spec v0.9 EPIC-18, ST-18-01..06) - additive, guarded: a wiring failure here
    # (e.g. a future migration mismatch) must never break Stage 1, which this module's callers
    # already depend on.
    try:
        from nox.rl.vision import install_vision

        install_vision(core)
    except Exception:  # noqa: BLE001 - see comment above
        log.warning("rl.vision_install_failed", exc_info=True)
    return RlRuntime(getattr(core, "rl_backfill_queue", None))


class RlRuntime:
    """Handle returned by `install()`; `NoxCore.stop()` awaits `stop()` so the replay backfill
    queue is cancelled before the database closes (it used to write into a closed DB)."""

    def __init__(self, backfill_queue: TaskQueue | None) -> None:
        self._queue = backfill_queue

    async def stop(self) -> None:
        if self._queue is not None:
            await self._queue.stop()


class _NullCalloutEngine:
    def evaluate(self, **_kwargs: Any) -> None:
        return None


def _install_backfill_task(core: Any, cfg: Any, replays: RlReplayRepository) -> None:
    """Backlog parse of the full replay folder (ST-12-02 AC), via a dedicated `TaskQueue` instance
    (core does not run one by default) so it pauses automatically while `rocket_league` mode is
    active (`GAME_MODES`/`mode_change_means_game`, already generic in `nox.core.tasks`)."""
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
            # Parsing is CPU-bound and synchronous; a backlog of thousands of replays used to
            # run entirely on the event loop and blocked the core for over a minute at boot
            # (2026-09-16). Parse off-loop and yield between files.
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
    core.rl_backfill_queue = queue


__all__ = ["install"]
