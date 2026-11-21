"""The update check a running node makes (first some minutes after the start, then about once a
day) and `nox-node update`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

from nox.core.logging import get_logger
from nox.node.device.agent.node_agent import NodeAgent
from nox.node.device.cli.runtime import NodeRuntime, session_refusal
from nox.node.device.cli_text import (
    command,
    t,
)
from nox.node.device.home import NodeHome
from nox.node.device.tools import PlatformNotifier
from nox.node.device.update.state import UpdateCheck
from nox.node.device.update.updater import NodeUpdater

log = get_logger(__name__)


STALE_HINT = (
    "Updates may be held back (a frozen mirror or network); check the project's releases page."
)


#: The first update check some minutes after the start, then about once a day.
FIRST_CHECK_S = 600.0


DAY_S = 86400.0


UPDATE_POLL_S = 30.0


def under_launcher() -> bool:
    """Started by `installer/start_node.pyw`, which starts the node again after an update."""
    return os.environ.get("NOX_NODE_LAUNCHER") == "1"


def _record_update(runtime: NodeRuntime, check: UpdateCheck, *, stale: int | None = None) -> bool:
    """`update.json` for `nox node status` and the tray; True the first time something needs a
    person (an update to confirm or install, or no newer signed release for a month) - that gets
    a notification."""
    path = runtime.home.path / "update.json"
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    announced = before.get("announced") if isinstance(before, dict) else None
    topic = check.version if check.status in ("needs_confirmation", "needs_installer") else None
    if topic is None and stale is not None:
        topic = f"stale-{stale // 30}"
    new = topic is not None and announced != topic
    record = {
        "status": check.status,
        "message": check.message,
        "version": check.version,
        "stale_days": stale,
        "at": datetime.now(UTC).isoformat(),
        "announced": topic if new else announced,
    }
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return new


def core_range(home: NodeHome) -> Callable[[], tuple[int, int] | None]:
    """The node protocols the paired core accepted at the last connection."""

    def read() -> tuple[int, int] | None:
        identity = home.load_identity()
        return identity.core_node_protocol if identity is not None else None

    return read


def node_updater(home: NodeHome) -> NodeUpdater:
    return NodeUpdater(home.app_dir, core_range=core_range(home))


async def keep_updated(  # noqa: PLR0913 - the loop's collaborators, all needed
    runtime: NodeRuntime,
    agent: NodeAgent,
    updater: NodeUpdater,
    stop: asyncio.Event,
    restart: asyncio.Event,
    *,
    poll_s: float = UPDATE_POLL_S,
    trial_failed: asyncio.Event | None = None,
) -> None:
    """Confirm a version on trial once its core accepted it (or end it when the core refused its
    protocol); check the signed release about once a day; restart into a new version when one is
    in place and nothing runs. One failing round never ends the loop."""
    confirmed = False
    next_check = time.monotonic() + FIRST_CHECK_S
    failures = 0
    while not stop.is_set():
        await asyncio.sleep(poll_s * (1 + min(failures, 6)))
        try:
            if not confirmed:
                confirmed = await _confirm(agent, updater, stop, trial_failed)
                if stop.is_set():
                    return
            if confirmed and under_launcher() and updater.restart_needed() and not agent.busy:
                restart.set()
                stop.set()
                return
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + DAY_S * (0.9 + 0.2 * random.random())  # noqa: S311
                await _daily(runtime, updater, confirmed=confirmed)
            failures = 0
        except Exception as exc:  # noqa: BLE001 - logged; the next round tries again, later
            failures += 1
            log.warning("node.update_round_failed", error=f"{type(exc).__name__}: {exc}"[:200])


async def _confirm(
    agent: NodeAgent,
    updater: NodeUpdater,
    stop: asyncio.Event,
    trial_failed: asyncio.Event | None,
) -> bool:
    """True once the running version is no (longer a) trial. A version on trial counts as
    working only when its core accepted it; a core that refused its protocol ends the trial."""
    if not await asyncio.to_thread(updater.on_trial):
        return True
    if agent.connected.is_set():
        # An error here propagates: the next round tries again, nothing is marked bad.
        await asyncio.to_thread(updater.confirm_running)
        return True
    if agent.protocol_refused and under_launcher():
        log.error("node.trial_refused_by_core")
        if trial_failed is not None:
            trial_failed.set()
        stop.set()
    return False


async def _daily(runtime: NodeRuntime, updater: NodeUpdater, *, confirmed: bool) -> None:
    check = await asyncio.to_thread(updater.check)
    if check.status == "refused":
        log.warning("node.update_refused", reason=check.message[:200])
    stale = updater.stale_days()
    if stale is not None:
        log.warning("node.release_info_stale", days=stale)
    if _record_update(runtime, check, stale=stale):
        with contextlib.suppress(Exception):
            await PlatformNotifier().show("Nox: Update", _notice(check, stale))
    if check.status == "available" and under_launcher() and confirmed:
        result = await asyncio.to_thread(updater.apply, check)
        _record_update(runtime, result, stale=stale)


def _notice(check: UpdateCheck, stale: int | None) -> str:
    if stale is not None and check.status not in ("needs_confirmation", "needs_installer"):
        return f"No newer signed Nox release seen for {stale} days. {STALE_HINT}"
    return check.message


def update(
    home: NodeHome,
    *,
    check_only: bool,
    yes: bool,
    say: Callable[[str], None],
    ask: Callable[[str], bool],
) -> int:
    """`nox node update`: 0 done or up to date, 1 refused, 3 release signing not configured.
    `yes` also tries again a version that failed here before."""
    updater = node_updater(home)
    check = updater.check(retry_bad=yes)
    say(check.message)
    stale = updater.stale_days()
    if stale is not None:
        say(t("update_stale", days=stale))
    if check.status == "not_configured":
        return 3
    if check.status == "refused":
        return 1
    if check_only or check.status not in ("available", "needs_confirmation"):
        return 0
    refusal = session_refusal(home)
    if refusal is not None:
        # A new version can change what this device does: not while Nox controls it.
        say(refusal)
        return 1
    if not yes and not ask(t("update_ask", version=check.version)):
        say(t("update_not_installed"))
        return 0
    result = updater.apply(check, confirmed=True)
    say(result.message)
    if result.status != "installed":
        return 1
    say(
        t("update_restart_soon")
        if (home.path / "status.json").exists()
        else t("update_start_by_hand", run=command("run"))
    )
    return 0
