"""`nox node pair | run | status | log | allow | deny` - the node's own terminal commands.

Everything here runs as the logged-in user and touches only that user's node folder
(`nox.node.home`). `allow` is the one way to widen what Nox may do on this device; nothing the
core sends can do it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nox.core.logging import get_logger
from nox.node.agent import NodeAgent
from nox.node.confirmed import (
    MOST_RESTRICTIVE,
    ConfirmationKey,
    effective_manifest,
    last_confirmed,
    manifest_key,
    record_confirmed,
)
from nox.node.enroll import EnrollError, enroll
from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, default_sandbox, hand_tools
from nox.node.home import NodeHome, restrict_dir_to_owner
from nox.node.keystore import NodeKeyStore
from nox.node.manifest import GROUPS, NodeManifest, load_manifest, save_manifest
from nox.node.protocol import PairLink, PairLinkError, escape_controls
from nox.node.tools import PlatformNotifier, default_tools
from nox.node.update import NodeUpdater, UpdateCheck
from nox.node.world import World, world_tools
from nox.sandbox import Sandbox
from nox.security.audit import SqliteAuditLog
from nox.security.model import SecretStore
from nox.security.secrets import KeyringSecretStore, keyring_usable

__all__ = [
    "RESTART",
    "TRIAL_FAILED",
    "NodeCliError",
    "NodeRuntime",
    "allow",
    "build",
    "log_lines",
    "pair",
    "run",
    "status",
]

log = get_logger(__name__)

KEYRING_SERVICE = "nox-node"
#: What `run` returns when the node should start again (a new version is in place) ...
RESTART = "restart"
#: ... and when its core refused the protocol of the version on trial (the launcher rolls back).
TRIAL_FAILED = "trial_failed"
#: The node's marker of an orderly end, next to `state.json` (`installer/start_node.pyw`).
CLEAN_EXIT = "clean-exit"
STALE_HINT = (
    "Updates may be held back (a frozen mirror or network); check the project's releases page."
)
#: The first update check some minutes after the start, then about once a day.
FIRST_CHECK_S = 600.0
DAY_S = 86400.0
UPDATE_POLL_S = 30.0


class NodeCliError(RuntimeError):
    """A message for the person at the terminal."""


@dataclass
class NodeRuntime:
    home: NodeHome
    keystore: NodeKeyStore
    gate: NodeGate
    audit: SqliteAuditLog
    manifest_key: ConfirmationKey | None = None
    #: The C6 tools' device side (captures, browser, desktop, computer use, media).
    world: World | None = None


def secret_store() -> SecretStore | None:
    """The OS keyring when there is one (Windows, macOS, a desktop Linux); None on a headless
    Linux node, whose key file is then protected by its file mode alone."""
    return KeyringSecretStore(service=KEYRING_SERVICE) if keyring_usable() else None


def open_audit(home: NodeHome) -> SqliteAuditLog:
    home.ensure()
    return SqliteAuditLog(sqlite3.connect(home.audit_file, check_same_thread=False))


def build(
    home: NodeHome,
    *,
    secrets: SecretStore | None = None,
    notifier: object = None,
    say: Callable[[str], None] | None = None,
    sandbox: Callable[[], Sandbox | None] | None = None,
) -> NodeRuntime:
    home.ensure()
    if sys.platform == "win32":
        # On every start, not only when the folder was made: an ACL widened since is put back.
        restrict_dir_to_owner(home.path)
    audit = open_audit(home)
    key = manifest_key(home.path, secrets)
    manifest = effective_manifest(load_manifest(home.manifest_file), audit, key, say=say)
    holder: list[NodeGate] = []
    hands = NodeHands(
        # Read per call: `nox node deny files --folder` applies to the running node at once.
        folders=lambda: list(holder[0].manifest.folders) if holder else [],
        protected=(home.path,),
        sandbox=sandbox or default_sandbox,
    )
    shown = notifier if notifier is not None else PlatformNotifier()

    async def notice(text: str) -> None:
        await shown.show("Nox", text)  # type: ignore[attr-defined]

    def computer_enabled() -> bool:
        gate = holder[0] if holder else None
        return (
            gate is not None
            and gate.stop_state() is None
            and "computer" in gate.manifest.effective(gate.narrowed)
        )

    def audit_computer(action: str, target: str, result: str) -> None:
        audit.append(
            actor="core",
            tool="computer",
            action=action,
            target=target[:200],
            decision="allow" if result == "ok" else "deny",
            result=result,
        )

    world = World(
        notice=notice,
        computer_enabled=computer_enabled,
        dialog_open=lambda: bool(holder) and holder[0].dialog_open(),
        browser_dir=home.path / "browser",
        audit=audit_computer,
    )
    tools = {
        **default_tools(notifier),  # type: ignore[arg-type]
        **hand_tools(hands),
        **world_tools(world),
    }
    gate = NodeGate(tools, manifest, audit, protected=home.path)
    gate.input_lock = world.lock.reason  # the game lock, before anything else (spec C §1.6)
    holder.append(gate)
    keystore = NodeKeyStore(home.key_file, secrets)
    gate.restore_budget(audit)  # a restart does not refill the hour's budget
    return NodeRuntime(home, keystore, gate, audit, key, world)


def manifest_source(runtime: NodeRuntime) -> Callable[[], NodeManifest]:
    """Re-read `node.yaml` for the running node: narrowing applies at once, widening only as
    far as it was confirmed in a terminal."""

    def read() -> NodeManifest:
        return effective_manifest(
            load_manifest(runtime.home.manifest_file), runtime.audit, runtime.manifest_key
        )

    return read


_REASONS = {
    "fingerprint_mismatch": "The device answering is not the Nox from the link (its "
    "fingerprint differs). Nothing was sent. Pair only in your own network.",
    "unreachable": "Nox is not reachable at the address in the link.",
    "already_paired": "This device is already paired. Remove it on the core first (Devices page).",
    "used": "The code was already used. Make a new one.",
    "expired": "The code has expired. Make a new one.",
    "unknown_code": "Nox does not know this code.",
    "max_devices": "Nox has as many nodes as it accepts.",
    "rate_limited": "Too many attempts. Wait a minute.",
    "name_taken": "Name ähnelt einem vorhandenen Gerät - the name looks like a device Nox already "
    "has. Choose another one with --name.",
    "invalid_characters": "Bitte nur Buchstaben, Ziffern, Leerzeichen und - _ . "
    "(letters A-Z, ä ö ü ß, digits, space, - _ .). Choose another name with --name.",
    "not_nox": "The device at that address does not answer like Nox.",
    "key_not_protected": "The key file could not be made readable by this user only.",
}


def pair(text: str, *, home: NodeHome, name: str = "", secrets: SecretStore | None = None) -> str:
    try:
        link = PairLink.parse(text)
    except PairLinkError as exc:
        raise NodeCliError(f"Not a Nox node link: {exc}") from exc
    keystore = NodeKeyStore(home.key_file, secrets)
    try:
        identity = asyncio.run(enroll(link, home, keystore, name=name))
    except EnrollError as exc:
        raise NodeCliError(_REASONS.get(exc.reason, f"Pairing failed: {exc.reason}")) from exc
    # The defaults, written and recorded as what this device confirmed at pairing.
    confirm_defaults(home, secrets)
    return f"Paired as {identity.name!r}. Start it with: nox node run"


def confirm_defaults(home: NodeHome, secrets: SecretStore | None) -> NodeManifest:
    """At pairing only the defaults are confirmed - never whatever a `node.yaml` that was there
    before says. What the file widened is dropped from it."""
    found = load_manifest(home.manifest_file)
    defaults = NodeManifest()
    start = NodeManifest(
        groups={g: bool(found.groups.get(g) and defaults.groups[g]) for g in GROUPS},
        max_risk=min(
            found.max_risk, defaults.max_risk, key=("read", "low", "medium", "high").index
        ),
        trust_core_for_high=False,
    )
    save_manifest(home.manifest_file, start)
    if not record_confirmed(
        open_audit(home), start, manifest_key(home.path, secrets), by="pairing", create=True
    ):
        raise NodeCliError("No confirmation key could be stored on this device.")
    return start


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
        if runtime.world is not None:
            agent.on_stop = runtime.world.stop  # a stop ends a computer session, closes the browser
            agent.session_report = runtime.world.session_report
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
        from nox.node.tray import spawn_tray  # noqa: PLC0415 - starts nothing when unavailable

        tray_process = spawn_tray() if tray else None
        try:
            outcome = await agent.run(stop)
        finally:
            updates.cancel()
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
        "not_paired": "This device is not paired yet: nox node pair <link>",
        "revoked": "Removed from Nox (Von Nox entfernt). Key and certificate are deleted.",
        "pin_mismatch": "The stored certificate authority is not the pinned one; pair again.",
        "stopped": "Stopped.",
        RESTART: RESTART,
        TRIAL_FAILED: TRIAL_FAILED,
    }.get(outcome, outcome)


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
        say(f"Warning: no newer signed release seen for {stale} days. {STALE_HINT}")
    if check.status == "not_configured":
        return 3
    if check.status == "refused":
        return 1
    if check_only or check.status not in ("available", "needs_confirmation"):
        return 0
    if not yes and not ask(f"Install Nox {check.version} on this device now?"):
        say("Not installed.")
        return 0
    result = updater.apply(check, confirmed=True)
    say(result.message)
    if result.status != "installed":
        return 1
    say(
        "The node starts the new version within a minute."
        if (home.path / "status.json").exists()
        else "Start the node: nox node run"
    )
    return 0


def status(home: NodeHome) -> list[str]:
    identity = home.load_identity()
    if identity is None:
        last = home.read_status().get("state")
        return ["Removed from Nox." if last == "removed" else "Not paired."]
    manifest = load_manifest(home.manifest_file)
    state = home.read_status()
    allowed = [GROUPS[g].label_en for g in GROUPS if manifest.groups.get(g)]
    x = escape_controls
    lines = [
        f"Name:      {x(identity.name)}",
        f"Core:      {x(identity.core_host)}:{identity.node_port}",
        f"State:     {x(str(state.get('state', 'not running')))} ({x(str(state.get('at', '-')))})",
        f"Nox may:   {', '.join(allowed) or 'nothing'}",
        f"Max risk:  {manifest.max_risk}",
        f"Folders:   {', '.join(x(f) for f in manifest.folders) or 'none'}",
    ]
    try:
        update_info = json.loads((home.path / "update.json").read_text(encoding="utf-8"))
        lines.append(f"Update:    {x(str(update_info.get('message', '-')))[:160]}")
        stale = update_info.get("stale_days")
        if isinstance(stale, int):
            lines.append(f"Warning:   no newer signed release seen for {stale} days. {STALE_HINT}")
    except (OSError, ValueError, AttributeError):
        pass
    return lines


def log_lines(home: NodeHome, *, limit: int = 50) -> list[str]:
    audit = open_audit(home)
    entries = audit.entries(since_seq=max(0, audit.count() - limit), limit=limit)
    # Every field is escaped on the way out too: an entry written before this check existed,
    # or by anything else, still cannot drive the terminal or fake a line.
    x = escape_controls
    lines = [
        f"{e.seq:>6}  {x(e.decision):<5}  {x(e.actor):<6} {x(e.tool):<14} {x(e.action):<12} "
        f"{x(e.target)} {x(e.result)}".rstrip()
        for e in entries
    ]
    lines.append("Chain intact." if audit.verify_chain() else "CHAIN BROKEN - the log was altered.")
    return lines


def _local_folder(text: str, node_home: NodeHome, *, must_exist: bool = True) -> str:
    """A folder typed in a terminal here: absolute, local, existing, resolved once, now - and
    never the node's own folder or one inside it."""
    from nox.node.gate import is_remote_or_device_path  # noqa: PLC0415

    raw = text.strip()
    if not raw or is_remote_or_device_path(raw) or not Path(raw).is_absolute():
        raise NodeCliError("Give an absolute folder on this device (no network path).")
    real = Path(os.path.realpath(raw))
    if must_exist and not real.is_dir():
        raise NodeCliError(f"{escape_controls(str(real))} is not a folder.")
    home = Path(os.path.realpath(node_home.path))
    if real == home or home in real.parents:
        raise NodeCliError("The node's own folder can never be opened by Nox.")
    if must_exist:
        from nox.node.safe_fs import broad_folder, secret_locations  # noqa: PLC0415

        if broad_folder(str(real)):
            raise NodeCliError(
                "That folder is too broad (a drive, your home folder, a system or app-data "
                "folder). Allow a folder inside it, for example one for Nox's files."
            )
        for place in secret_locations():
            secret = Path(os.path.realpath(place))
            if real == secret or secret in real.parents or real in secret.parents:
                raise NodeCliError("That folder holds or lies in a store of keys or passwords.")
    return str(real)


#: Not a group, but confirmed the same way: on a device without a screen, let high risk run when
#: a person confirmed it on the core (spec C §1.6 step 5, a documented residual risk).
TRUST_SETTING = "trust_core_for_high"
TRUST_LABEL = "run high-risk actions a person confirmed on the core (no screen here)"
#: The second question before `computer` is enabled on a device.
COMPUTER_WARNING = (
    "Nox will be able to move the mouse and press keys on this device, in sessions you allow "
    "here one by one (never while a game or an anti-cheat service runs; Ctrl+Alt+Shift+K stops "
    "it at once). Really allow?"
)


def computer_supported() -> bool:
    """Computer use on a node exists on Windows only (spec C §1.4)."""
    return sys.platform == "win32"


def allow(
    home: NodeHome,
    group: str,
    enabled: bool,
    *,
    say: Callable[[str], None],
    interactive: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
    confirm: Callable[[str], bool] = lambda _prompt: False,
    secrets: SecretStore | None = None,
    folder: str = "",
) -> None:
    """Change one group. Widening asks in an interactive terminal for a typed "yes", and the
    new manifest is the last confirmed one plus this change, so unconfirmed edits of `node.yaml`
    are never adopted.

    The terminal check keeps the *core* and remote tools from widening a node - nothing they send
    reaches a terminal. It is NOT a boundary against a local process of the same user, which can
    open a terminal of its own; a real presence check (Windows Hello / UAC consent, polkit) is a
    later task (SECURITY.md, residual risk)."""
    trust = group == TRUST_SETTING
    if group not in GROUPS and not trust:
        known = ", ".join([*GROUPS, TRUST_SETTING])
        raise NodeCliError(f"Unknown group {group!r}. Known: {known}")
    if folder and group != "files":
        raise NodeCliError("--folder belongs to the group 'files'.")
    if folder:
        folder = _local_folder(folder, home, must_exist=enabled)
    label = TRUST_LABEL if trust else GROUPS[group].label_en
    if folder:
        label = f"open files in {escape_controls(folder)}"
    audit = open_audit(home)
    if enabled:
        if not interactive():
            raise NodeCliError("Allowing needs an interactive terminal on this device.")
        if group == "computer" and not computer_supported():
            raise NodeCliError("Mouse and keyboard control on a node is available on Windows only.")
        prompt = f"Let Nox {label} on this device?"
        if not confirm(prompt):
            raise NodeCliError("Not allowed.")
        if group == "computer" and not confirm(COMPUTER_WARNING):
            # Asked twice, the second time with what it means (owner decision 2026-10-18).
            raise NodeCliError("Not allowed.")
    key = manifest_key(home.path, secrets)
    manifest = last_confirmed(audit, key) or NodeManifest(groups=dict(MOST_RESTRICTIVE.groups))
    if trust:
        manifest.trust_core_for_high = enabled
    elif folder:
        # A folder alone widens nothing while the group is off; denying one keeps the group.
        rest = [f for f in manifest.folders if f != folder]
        manifest.folders = [*rest, folder] if enabled else rest
    else:
        manifest.groups[group] = enabled
    save_manifest(home.manifest_file, manifest)
    if not record_confirmed(audit, manifest, key, create=enabled):
        if enabled:
            raise NodeCliError("No confirmation key could be stored on this device.")
    audit.append(
        actor="local",
        tool="node",
        action="node.allow" if enabled else "node.deny",
        target=f"files:{folder}"[:200] if folder else group,
        decision="allow",
        result="ok",
    )
    say(f"{'Allowed' if enabled else 'Denied'}: {label}.")
