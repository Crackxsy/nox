"""The satellites on a node, put together (C9): voice, OBS and the overlay relay.

`build_satellites` makes them from the node's folder and gate; `attach` connects them to the
node's agent (what the core sends them, the connection coming and going, a stop); `run` keeps
the voice satellite and the overlay relay going until the node stops. Each one asks the gate
before it does anything: the groups this device enabled, the core's narrowing, a stop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nox.core.logging import get_logger
from nox.node import protocol as p
from nox.node.home import NodeHome
from nox.node.obs import OBS_PANIC_FILE, OBS_SECRET, NodeObs, obs_tools
from nox.node.overlay import OverlayRelay
from nox.node.satellite_settings import SATELLITES_FILE, SettingsFile
from nox.node.tools import NodeTool
from nox.node.voice import VoiceSatellite, voice_tools
from nox.security.model import AuditLog, SecretStore

log = get_logger(__name__)

__all__ = ["NodeSatellites", "build_satellites"]

#: How often the relay looks whether `obs` is still enabled on this device.
OVERLAY_POLL_S = 1.0


@dataclass
class NodeSatellites:
    home: NodeHome
    settings: SettingsFile
    voice: VoiceSatellite
    obs: NodeObs
    overlay: OverlayRelay
    #: Whether `group` may act right now: enabled here, not narrowed, the node not stopped.
    group_on: Callable[[str], bool]
    #: Whether `group` is enabled on this device at all (narrowed by the core or not).
    group_enabled: Callable[[str], bool]
    agent: Any = None
    _background: set[asyncio.Task[Any]] = field(default_factory=set)

    def tools(self) -> dict[str, NodeTool]:
        return {**voice_tools(self.voice), **obs_tools(self.obs)}

    # -- the agent -------------------------------------------------------------------------------

    def attach(self, agent: Any) -> None:
        self.agent = agent
        agent.satellites[p.M_VOICE_SAY] = self.voice.on_say
        agent.satellites[p.M_VOICE_CANCEL] = self.voice.on_cancel
        agent.satellites[p.M_VOICE_GATE] = self.voice.on_gate
        agent.satellites[p.M_OBS_PANIC] = self._panic
        agent.on_connection.append(self._connection)

    def features(self) -> frozenset[str]:
        agent = self.agent
        return frozenset(agent.features) if agent is not None else frozenset()

    def connected(self) -> bool:
        agent = self.agent
        return agent is not None and agent.connected.is_set() and bool(agent.compatible)

    def _connection(self, connected: bool) -> None:
        self.voice.connection(connected)
        # A new connection gets a new credential (the old one is revoked by the core then).
        self.overlay.forget_credential()
        if not connected:
            self._spawn(self.overlay.close_all("not connected"))

    async def _panic(self, params: dict[str, Any]) -> None:
        """`nox/obs/panic`: protective, so it acts while `obs` is enabled here at all - even
        narrowed by the core. In the background: the agent's channel never waits for OBS."""
        if not self.group_enabled("obs"):
            return
        reason = str(params.get("reason", ""))[:64]
        self._spawn(self.obs.panic(reason))

    def stopped(self, reason: str) -> None:
        """A stop on this node: nothing more is said; the overlay pages go dark."""
        self.voice.stopped(reason)
        self._spawn(self.overlay.close_all("stopped"))

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- running ---------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.gather(self.voice.run(stop), self._overlay_loop(stop))
        finally:
            await self.obs.close()

    async def _overlay_loop(self, stop: asyncio.Event) -> None:
        """Serve the relay while `obs` is enabled on this device (offline pages included); stop
        serving when it is not."""
        while not stop.is_set():
            if not self.group_enabled("obs"):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), OVERLAY_POLL_S)
                continue
            off = asyncio.Event()

            async def watch(off: asyncio.Event = off) -> None:
                while not stop.is_set() and self.group_enabled("obs"):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), OVERLAY_POLL_S)
                off.set()

            watcher = asyncio.create_task(watch())
            try:
                await self.overlay.serve(off)
            except (OSError, SystemExit) as exc:  # the port is taken: said, tried again later
                log.warning("node.overlay_not_served", error=type(exc).__name__)
                off.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 30.0)
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher


def build_satellites(
    home: NodeHome,
    *,
    gate: Callable[[], Any],
    secrets: SecretStore | None,
    audit: AuditLog | None,
) -> NodeSatellites:
    """The satellites of this node. `gate` returns the node's `NodeGate` (made after the tools)."""
    settings = SettingsFile(home.path / SATELLITES_FILE)
    holder: list[NodeSatellites] = []

    def group_enabled(group: str) -> bool:
        current = gate()
        return current is not None and group in current.manifest.enabled()

    def group_on(group: str) -> bool:
        current = gate()
        return (
            current is not None
            and current.stop_state() is None
            and group in current.manifest.effective(current.narrowed)
        )

    def note(action: str, result: str) -> None:
        if audit is not None:
            with contextlib.suppress(Exception):
                audit.append(
                    actor="local",
                    tool="voice",
                    action=action,
                    target="",
                    decision="allow",
                    result=result[:120],
                )

    def stop_here(reason: str) -> None:
        home.ensure()
        home.stop_file.write_text(f"{reason}\n", encoding="utf-8")

    def satellites_connected() -> bool:
        return bool(holder) and holder[0].connected()

    def features() -> frozenset[str]:
        return holder[0].features() if holder else frozenset()

    async def ask(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        agent = holder[0].agent if holder else None
        if agent is None:
            return None
        answer: dict[str, Any] | None = await agent.ask_core(method, params)
        return answer

    async def tell(method: str, params: dict[str, Any]) -> None:
        agent = holder[0].agent if holder else None
        if agent is not None:
            await agent.notify_core(method, params)

    voice = VoiceSatellite(
        home=home.path,
        settings=lambda: settings.read().voice,
        allowed=lambda: (
            group_on("voice") and satellites_connected() and p.FEATURE_VOICE in features()
        ),
        ask_core=ask,
        tell_core=tell,
        stop_here=stop_here,
        audit=note,
    )

    def password() -> str | None:
        if secrets is None:
            return None
        try:
            return secrets.get(OBS_SECRET)
        except Exception:  # noqa: BLE001 - no keyring: OBS without a password, or it says no
            return None

    obs = NodeObs(
        settings=lambda: settings.read().obs,
        password=password,
        panic_file=home.path / OBS_PANIC_FILE,
    )

    async def credential() -> str | None:
        answer = await ask(p.M_OVERLAY_CREDENTIAL, {})
        result = answer.get("result") if isinstance(answer, dict) else None
        token = result.get("token") if isinstance(result, dict) else None
        return token if isinstance(token, str) and len(token) >= 16 else None

    overlay = OverlayRelay(
        identity=home.load_identity,
        ca_file=home.ca_file,
        allowed=lambda: (
            group_on("obs") and satellites_connected() and p.FEATURE_OVERLAY in features()
        ),
        credential=credential,
        settings=lambda: settings.read().overlay,
    )
    satellites = NodeSatellites(
        home=home,
        settings=settings,
        voice=voice,
        obs=obs,
        overlay=overlay,
        group_on=group_on,
        group_enabled=group_enabled,
    )
    holder.append(satellites)
    return satellites
