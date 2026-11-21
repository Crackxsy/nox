"""What every terminal command shares: the message type for the person at the terminal, the refusal
while a computer-use session runs, the secret store, the audit log, the node's parts
(`NodeRuntime`) and `build`, which wires them together."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass

from nox.core.logging import get_logger
from nox.node.device.cli_text import (
    command,
    t,
)
from nox.node.device.confirmed import (
    ConfirmationKey,
    effective_manifest,
    manifest_key,
)
from nox.node.device.gate import NodeGate
from nox.node.device.hands.catalogue import hand_tools
from nox.node.device.hands.node_hands import NodeHands, default_sandbox
from nox.node.device.home import NodeHome, restrict_dir_to_owner
from nox.node.device.keystore import NodeKeyStore
from nox.node.device.satellite_node import NodeSatellites, build_satellites
from nox.node.device.tools import PlatformNotifier, default_tools
from nox.node.device.world.catalogue import world_tools
from nox.node.device.world.game_lock import GameLock, default_games
from nox.node.device.world.node_world import World
from nox.node.device.world.notices import SESSION_MARKER, session_marker_state
from nox.node.wire.manifest import NodeManifest, load_manifest
from nox.sandbox import Sandbox
from nox.security.audit import SqliteAuditLog
from nox.security.model import SecretStore
from nox.security.secrets import KeyringSecretStore, keyring_usable

log = get_logger(__name__)


KEYRING_SERVICE = "nox-node"


class NodeCliError(RuntimeError):
    """A message for the person at the terminal."""


def session_refusal(home: NodeHome) -> str | None:
    """Why a command that widens what Nox may do here refuses now: a computer-use session runs
    on this device (or whether one runs cannot be read), or None."""
    from nox.core.config.hands import ComputerUseConfig  # noqa: PLC0415

    state = session_marker_state(home.path)
    if state is None:
        return None
    stop = ComputerUseConfig().stop_hotkey
    if state == "running":
        return t("session_refused", stop=stop, cmd=command("stop"))
    return t("session_unreadable", path=str(home.path / SESSION_MARKER), cmd=command("stop"))


def refuse_during_session(home: NodeHome) -> None:
    refusal = session_refusal(home)
    if refusal is not None:
        raise NodeCliError(refusal)


@dataclass
class NodeRuntime:
    home: NodeHome
    keystore: NodeKeyStore
    gate: NodeGate
    audit: SqliteAuditLog
    manifest_key: ConfirmationKey | None = None
    #: The device side of the world tools (captures, browser, desktop, computer use, media).
    world: World | None = None
    #: The satellites (voice, OBS, the overlay relay).
    satellites: NodeSatellites | None = None


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

    extra_games: set[str] = set()

    def watched() -> list[str]:
        # The core's defaults, this device's own list and what the core added - read per call.
        own = list(holder[0].manifest.games) if holder else []
        return [*default_games(), *own, *sorted(extra_games)]

    world = World(
        notice=notice,
        computer_enabled=computer_enabled,
        dialog_open=lambda: bool(holder) and holder[0].dialog_open(),
        browser_dir=home.path / "browser",
        audit=audit_computer,
        lock=GameLock(watched=watched),
        extra_games=extra_games,
        marker=home.path / SESSION_MARKER,
    )
    satellites = build_satellites(
        home, gate=lambda: holder[0] if holder else None, secrets=secrets, audit=audit
    )
    tools = {
        **default_tools(notifier),  # type: ignore[arg-type]
        **hand_tools(hands),
        **world_tools(world),
        **satellites.tools(),
    }
    gate = NodeGate(tools, manifest, audit, protected=home.path)
    gate.input_lock = world.lock.reason  # the game lock, before anything else
    holder.append(gate)
    keystore = NodeKeyStore(home.key_file, secrets)
    gate.restore_budget(audit)  # a restart does not refill the hour's budget
    return NodeRuntime(home, keystore, gate, audit, key, world, satellites)


MEDIA_NOTICE_MARKER = "media-notice-shown"


def media_notice() -> str:
    return t("media_notice", deny=command("deny media"))


async def media_notice_once(runtime: NodeRuntime) -> bool:
    """Music control is on by default: the first time a node runs with it on - a
    new pairing or an update that brought it -, this device says so once. True when shown."""
    marker = runtime.home.path / MEDIA_NOTICE_MARKER
    world = runtime.world
    if world is None or marker.exists() or "media" not in runtime.gate.manifest.enabled():
        return False
    try:
        await world.notice(media_notice())
    except Exception as exc:  # noqa: BLE001 - shown next time instead
        log.warning("node.media_notice_failed", error=type(exc).__name__)
        return False
    marker.write_text("shown", encoding="utf-8")
    return True


def manifest_source(runtime: NodeRuntime) -> Callable[[], NodeManifest]:
    """Re-read `node.yaml` for the running node: narrowing applies at once, widening only as
    far as it was confirmed in a terminal."""

    def read() -> NodeManifest:
        return effective_manifest(
            load_manifest(runtime.home.manifest_file), runtime.audit, runtime.manifest_key
        )

    return read
