"""`NodeRuntime`: what the node extension returns to the core, and the tasks it keeps running.

Owns the runtime object (listener, registry, satellites, the background tasks and their shutdown)
and the task that starts the node listener once the network listener has its certificates. It does
not wire anything onto the core; that is `nox.node.hub.install`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from nox.core.logging import get_logger
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.ports import LanPort
from nox.node.hub.registry import NodeRegistry
from nox.node.hub.satellites import SatelliteHub
from nox.node.hub.world_routes import NodeControl

log = get_logger(__name__)

__all__ = ["TLS_RECHECK_S", "TLS_WAIT_S", "NodeRuntime", "keep", "serve"]

#: How often the listener compares its certificates with the network listener's.
TLS_RECHECK_S = 60.0
#: How long the node listener waits for the network listener's first certificates.
TLS_WAIT_S = 30.0


@dataclass
class NodeRuntime:
    pairing: NodePairing
    enabled: bool
    listener: NodeListener | None = None
    registry: NodeRegistry | None = None
    #: The satellites' side: voice turns, overlay credentials, the microphone gate.
    satellites: SatelliteHub | None = None
    #: The computer-use lock: which node Nox controls right now, if any.
    control: NodeControl | None = None
    error: str = ""
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def running(self) -> bool:
        return self.listener is not None and self.listener.running

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.satellites is not None:
            await self.satellites.close()
        if self.registry is not None:
            await self.registry.close()
        if self.listener is not None:
            await self.listener.stop()


def keep(runtime: NodeRuntime, coro: Coroutine[Any, Any, Any], name: str) -> None:
    """Run `coro` as a task of the runtime: it ends with `runtime.stop()`."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    runtime.tasks.add(task)
    task.add_done_callback(runtime.tasks.discard)


async def serve(runtime: NodeRuntime, listener: NodeListener, lan: LanPort) -> None:
    """Start once the network listener has certificates, then follow its re-issues."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TLS_WAIT_S
    while lan.tls is None or not lan.running:
        if loop.time() > deadline:
            runtime.error = "network access did not start"
            log.error("node.listener_not_started", reason=runtime.error)
            return
        await asyncio.sleep(0.1)
    try:
        await listener.start()
    except (OSError, RuntimeError) as exc:
        runtime.error = f"{type(exc).__name__}: {exc}"
        log.error("node.listener_failed", error=runtime.error)
        return
    while True:
        await asyncio.sleep(TLS_RECHECK_S)
        try:
            await listener.reload()
        except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad round
            log.error("node.reload_failed", error=f"{type(exc).__name__}: {exc}")
