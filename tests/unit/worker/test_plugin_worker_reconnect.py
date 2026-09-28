"""A plugin worker that loses the hub comes back, or ends itself - never a zombie.

Real hub, real token store, real IPC client: the reconnect credential has to survive the whole
chain, which is exactly what a fake client would hide.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore
from nox.plugins.manager import PluginRegister
from nox.plugins.manifest import load_manifest
from nox.worker.hub_loss import EXIT_HUB_LOST
from nox.worker.plugin import PluginWorker
from tests.unit.ipc.conftest import SimpleBus
from tests.unit.plugins.conftest import write_manifest


class Plugin:
    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _hub(
    tokens: TokenStore, registry: RequestRegistry, runtime: Path, port: int = 0
) -> IpcHub:
    hub = IpcHub(HubSettings(port=port), tokens, registry, SimpleBus(), runtime)
    await hub.start()
    return hub


def _registry(registrations: list[str]) -> RequestRegistry:
    registry = RequestRegistry()

    async def register(ctx: RequestContext, p: PluginRegister) -> dict[str, Any]:
        registrations.append(ctx.client_id)
        # A core that allows everything this plugin needs; the plugin starts from this state.
        return {
            "ok": True,
            "config": {},
            "privacy_mode": "balanced",
            "capture": {"microphone": True, "camera": False, "screen": True, "cloud": True},
            "safe_mode": False,
        }

    async def heartbeat(_ctx: RequestContext, _p: Heartbeat) -> dict[str, Any]:
        return {"ok": True}

    registry.register("plugin.register", PluginRegister, register, roles=("plugin",))
    registry.register("worker.heartbeat", Heartbeat, heartbeat, roles=("plugin",))
    return registry


class Heartbeat(BaseModel):
    status: str = ""
    load: float | None = None


def _worker(tmp_path: Path, hub: IpcHub, token: str, plugin: Plugin) -> PluginWorker:
    from nox.ipc.client import IpcClient

    manifest = load_manifest(write_manifest(tmp_path / "plugins", "demo"), expected_id="demo")
    worker: PluginWorker

    client = IpcClient(
        hub.url,
        token,
        "plugin",
        "plugin:demo",
        reconnect=True,
        backoff_initial_s=0.05,
        on_connection_change=lambda connected: worker.on_connection_change(connected),
        on_reconnect_refused=lambda error: worker.on_reconnect_refused(error),
    )
    worker = PluginWorker(client=client, manifest=manifest, factory=lambda _api: plugin)
    return worker


async def test_a_dropped_plugin_reconnects_and_registers_again(tmp_path: Path) -> None:
    tokens = TokenStore()
    registrations: list[str] = []
    hub = await _hub(tokens, _registry(registrations), tmp_path)
    plugin = Plugin()
    worker = _worker(tmp_path, hub, tokens.issue_worker_token("plugin:demo"), plugin)
    task = asyncio.create_task(worker.run())
    try:
        await wait_until(lambda: registrations == ["plugin:demo"])

        await hub.disconnect("plugin:demo", "send queue overflow")  # what a stalled loop does

        await wait_until(lambda: len(registrations) == 2)
        assert hub.find_client("plugin:demo") is not None
        assert not task.done() and not plugin.stopped
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, 5)
        await hub.stop()


async def test_a_plugin_the_core_no_longer_knows_exits_instead_of_lingering(
    tmp_path: Path,
) -> None:
    tokens = TokenStore()
    registrations: list[str] = []
    registry = _registry(registrations)
    hub = await _hub(tokens, registry, tmp_path)
    port = hub.port
    plugin = Plugin()
    worker = _worker(tmp_path, hub, tokens.issue_worker_token("plugin:demo"), plugin)
    task = asyncio.create_task(worker.run())
    await wait_until(lambda: registrations == ["plugin:demo"])

    await hub.stop()  # the core went away; a new one never honours the old credential
    restarted = await _hub(TokenStore(), registry, tmp_path, port=port)
    try:
        await asyncio.wait_for(task, 10)
    finally:
        await restarted.stop()

    assert worker.exit_code == EXIT_HUB_LOST
    assert plugin.stopped  # its own connections were closed on the way out


async def test_a_plugin_cut_off_past_its_deadline_exits(tmp_path: Path) -> None:
    tokens = TokenStore()
    registrations: list[str] = []
    hub = await _hub(tokens, _registry(registrations), tmp_path)
    plugin = Plugin()
    worker = _worker(tmp_path, hub, tokens.issue_worker_token("plugin:demo"), plugin)
    expired = asyncio.Event()

    async def deadline_sleep(_seconds: float) -> None:
        await expired.wait()

    worker.hub_loss._sleep = deadline_sleep  # noqa: SLF001 - drive the deadline by hand
    task = asyncio.create_task(worker.run())
    await wait_until(lambda: registrations == ["plugin:demo"])

    await hub.stop()  # nothing listens any more: reconnects keep failing, never refused
    expired.set()
    await asyncio.wait_for(task, 10)

    assert worker.exit_code == EXIT_HUB_LOST
    assert plugin.stopped
