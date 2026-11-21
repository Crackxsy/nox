"""`nox-node run`: the node's main loop and what it returns when the node should start again."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from nox.core.logging import get_logger
from nox.node.device.agent.node_agent import NodeAgent
from nox.node.device.cli.runtime import NodeRuntime, manifest_source, media_notice_once
from nox.node.device.cli.updates import keep_updated, node_updater
from nox.node.device.cli_text import (
    command,
    t,
)
from nox.node.device.world.notices import clear_session_marker

log = get_logger(__name__)


#: What `run` returns when the node should start again (a new version is in place) ...
RESTART = "restart"


#: ... and when its core refused the protocol of the version on trial (the launcher rolls back).
TRIAL_FAILED = "trial_failed"


#: The node's marker of an orderly end, next to `state.json` (`installer/start_node.pyw`).
CLEAN_EXIT = "clean-exit"


def _log_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("node.update_loop_ended", error=repr(task.exception())[:200])


def run(runtime: NodeRuntime, *, install_signals: bool = True, tray: bool = False) -> str:
    async def main() -> str:
        stop = asyncio.Event()
        restart = asyncio.Event()
        trial_failed = asyncio.Event()
        if install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop.set)
        agent = NodeAgent(
            runtime.home,
            runtime.keystore,
            runtime.gate,
            runtime.audit,
            manifest_source=manifest_source(runtime),
        )
        # No session outlives the node's process: a marker left by a crash goes now.
        clear_session_marker(runtime.home.path)
        if runtime.world is not None:
            agent.on_stop = runtime.world.stop  # a stop ends a computer session, closes the browser
            agent.session_report = runtime.world.session_report
            agent.on_games = runtime.world.add_games
            await media_notice_once(runtime)
        satellites = runtime.satellites
        satellite_task: asyncio.Task[None] | None = None
        if satellites is not None:
            satellites.attach(agent)
            before = agent.on_stop

            def on_stop(reason: str) -> None:
                before(reason)
                satellites.stopped(reason)  # nothing more is said, the overlay goes dark

            agent.on_stop = on_stop
            satellite_task = asyncio.create_task(satellites.run(stop))
        updates = asyncio.create_task(
            keep_updated(
                runtime,
                agent,
                node_updater(runtime.home),
                stop,
                restart,
                trial_failed=trial_failed,
            )
        )
        updates.add_done_callback(_log_done)
        from nox.node.device.tray import spawn_tray  # noqa: PLC0415 - loaded only when a tray is started, and starts nothing when unavailable

        tray_process = spawn_tray() if tray else None
        try:
            outcome = await agent.run(stop)
        finally:
            updates.cancel()
            if satellite_task is not None:
                stop.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(satellite_task, 10)
            if tray_process is not None:
                with contextlib.suppress(OSError):
                    tray_process.terminate()
        if trial_failed.is_set():
            return TRIAL_FAILED
        return RESTART if restart.is_set() else outcome

    outcome = asyncio.run(main())
    # Ended in order (not a crash): the launcher counts only crashes against a new version.
    with contextlib.suppress(OSError):
        runtime.home.app_dir.mkdir(parents=True, exist_ok=True)
        (runtime.home.app_dir / CLEAN_EXIT).write_text(outcome, encoding="utf-8")
    return {
        "not_paired": t("not_paired", pair=command('pair "<link>"')),
        "revoked": t("revoked"),
        "pin_mismatch": t("pin_mismatch", again=command('pair --again "<link>"')),
        "stopped": t("stopped"),
        RESTART: RESTART,
        TRIAL_FAILED: TRIAL_FAILED,
    }.get(outcome, outcome)
