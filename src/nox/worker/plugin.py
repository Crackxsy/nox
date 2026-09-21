"""Plugin worker process (`python -m nox.worker --plugin <id>`).

Loads `plugins/<id>/manifest.yaml`, imports the manifest's `entry` (`package:callable`), builds a
`PluginApi` scoped to that manifest and calls `create(api)`. Then it registers with the core
(`plugin.register`, declaring the tools the plugin registered during `create`), heartbeats like any
other worker, answers `tool.call` from the core and stops within the kill switch's two-second ack
window on `plugin.stop`. This module owns one plugin process; it owns none of the voice path.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from nox.core.events import E
from nox.core.logging import get_logger
from nox.ipc.protocol import Envelope
from nox.plugins.api import PluginApi, PluginApiError, PrivacyView
from nox.plugins.manifest import MANIFEST_FILE, PluginManifest, load_manifest
from nox.util.aio import maybe_await
from nox.worker.heartbeat import heartbeat_loop

log = get_logger(__name__)

HEARTBEAT_S = 2.0
REPO_ROOT = Path(__file__).resolve().parents[3]


def plugins_dir() -> Path:
    """`NOX_PLUGINS_DIR` (set by the core when it spawns us) or `<repo>/plugins`."""
    configured = os.environ.get("NOX_PLUGINS_DIR")
    return Path(configured) if configured else REPO_ROOT / "plugins"


def load_entry(manifest: PluginManifest, plugin_dir: Path) -> Any:
    """Import `manifest.entry` (`package.module:callable`) from `plugins/<id>/src`.

    The plugin's source directory is appended to `sys.path`, never prepended: a plugin that ships
    `src/asyncio.py` or its own `src/nox/` must not be able to shadow the standard library or the
    core inside its own worker process.
    """
    src = plugin_dir / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.append(str(src))
    module_name, _, attribute = manifest.entry.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginApiError(f"cannot import plugin entry {manifest.entry!r}: {exc}") from exc
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise PluginApiError(f"plugin entry {manifest.entry!r} is not callable")
    return factory


class PluginWorker:
    """Hosts exactly one plugin; all boundaries are enforced by its `PluginApi`/the core."""

    def __init__(
        self,
        *,
        client: Any,
        manifest: PluginManifest,
        factory: Any,
        heartbeat_s: float = HEARTBEAT_S,
        api: PluginApi | None = None,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.factory = factory
        self.heartbeat_s = heartbeat_s
        self.privacy = PrivacyView()
        self.api = api or PluginApi(manifest=manifest, client=client, privacy=self.privacy)
        self.plugin: Any = None
        self.status = "starting"
        self._stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None

    # -- inbound -----------------------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.client.handle("tool.call", self._on_tool_call)
        self.client.handle("plugin.stop", self._on_plugin_stop)
        self.client.on(E.PRIVACY_MODE_CHANGED, self._on_privacy_mode)
        self.client.on(E.SECURITY_KILL_SWITCH, self._on_kill_switch)
        self.client.on(E.SYSTEM_STOPPING, self._on_system_stopping)

    async def _on_tool_call(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        name = str(payload.get("name", ""))
        data = payload.get("input") or {}
        if not isinstance(data, dict):
            raise ValueError("tool.call input must be an object")
        return await self.api.tools.call(name, data)

    async def _on_plugin_stop(self, payload: dict[str, Any], _req: Any = None) -> dict[str, Any]:
        reason = str(payload.get("reason", "stop"))
        log.warning("plugin.stop_requested", plugin=self.manifest.id, reason=reason)
        self.status = "stopping"
        self._stop.set()
        return {"ok": True, "plugin_id": self.manifest.id}

    async def _on_privacy_mode(self, env: Envelope) -> None:
        mode = env.payload.get("current")
        if isinstance(mode, str):
            try:
                self.privacy.set(mode)
            except ValueError:
                log.warning("plugin.privacy_mode_unknown", plugin=self.manifest.id)

    async def _on_kill_switch(self, _env: Envelope) -> None:
        self.status = "safe_mode"
        self._stop.set()

    async def _on_system_stopping(self, _env: Envelope) -> None:
        self._stop.set()

    # -- lifecycle ---------------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        await heartbeat_loop(
            self.client,
            self._stop,
            status=lambda: self.status,
            interval_s=self.heartbeat_s,
            log_event="plugin.heartbeat_failed",
            plugin=self.manifest.id,
        )

    async def run(self) -> None:
        self._register_handlers()
        await self.client.connect()
        patterns = sorted(
            {"security.*", "privacy.*", "system.stopping", *self.manifest.events.listens}
        )
        await self.client.subscribe(patterns)
        # `create(api)` registers tools/handlers; events may only be emitted after `plugin.register`
        # has declared the plugin's namespaces to the hub, i.e. from `start()` onwards.
        self.plugin = plugin = await maybe_await(self.factory(self.api))
        response = await self.client.request(
            "plugin.register",
            {
                "plugin_id": self.manifest.id,
                "version": self.manifest.version,
                "pid": os.getpid(),
                "tools": self.api.tools.declarations(),
            },
        )
        config = response.get("config")
        if isinstance(config, dict):
            self.api.config.update(config)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"plugin-heartbeat-{self.manifest.id}"
        )
        self.status = "running"
        await self._call_plugin(plugin, "start")
        log.info("plugin.worker_running", plugin=self.manifest.id, tools=self.api.tools.names())
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    @staticmethod
    async def _call_plugin(plugin: Any, hook: str) -> None:
        """Call an optional `start`/`stop` hook, whether the plugin defined it `def` or `async`."""
        fn = getattr(plugin, hook, None)
        if fn is None:
            return
        await maybe_await(fn())

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.status = "stopping"
        self._stop.set()
        if self.plugin is not None:
            try:
                await self._call_plugin(self.plugin, "stop")
            except Exception as exc:  # noqa: BLE001 - a failing plugin must not block shutdown
                log.warning("plugin.stop_failed", plugin=self.manifest.id, error=str(exc))
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        await self.client.close()


async def run_plugin_worker(plugin_id: str, hub_url: str, token: str) -> int:
    """Process entry point for `python -m nox.worker --plugin <id>`.

    Returns 0 for a clean stop and 1 when the worker ended abnormally, so the supervisor can tell
    a requested shutdown from a crash.
    """
    from nox.ipc.client import IpcClient

    directory = plugins_dir() / plugin_id
    manifest = load_manifest(directory / MANIFEST_FILE, expected_id=plugin_id)
    factory = load_entry(manifest, directory)
    client = IpcClient(
        hub_url,
        token,
        "plugin",
        f"plugin:{plugin_id}",
        client_version="0.1.0",
        request_timeout_s=30.0,
    )
    worker = PluginWorker(client=client, manifest=manifest, factory=factory)
    try:
        await worker.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await worker.shutdown()
        return 1
    return 0
