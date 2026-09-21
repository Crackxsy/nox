"""PluginManager: discovery, validation, worker lifecycle and the core-side plugin IPC handlers.

Implements the plugin lifecycle - `discovered -> validated -> enabled -> spawned -> registered ->
running -> stopping -> stopped | failed`. A plugin is only spawned when its manifest validates
against the active security profile (namespace, secrets, hard prohibitions, egress); it runs in its
own `python -m nox.worker --plugin <id>` process with a one-time token inside the core's job
object; crashes are restarted with backoff (three tries in five minutes, then `failed`); and the
kill switch stops every plugin (`plugin.stop`, two seconds, then terminate). One misconfigured
plugin never blocks another.

Core request handlers, for the `plugin` role only: `plugin.register`, `plugin.secret.get`,
`plugin.tool.call`. Registered tools are mirrored into the core's `ToolRegistry`; every call is
checked by the permission engine first and then forwarded to the owning worker as a `tool.call`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from nox.core.events import E, Event, EventBus, HealthStatus, PluginLifecycle
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import ERR_INTERNAL, ERR_NOT_FOUND, ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.plugins.manifest import (
    ManifestError,
    PluginManifest,
    check_egress,
    discover_manifest_paths,
    load_manifest,
    validate_tool_name,
)
from nox.security.model import Decision, PermissionRequest, Profile, Risk

log = get_logger(__name__)

PLUGIN_CLIENT_PREFIX = "plugin:"
WORKER_STOP_REQUEST = "plugin.stop"
WORKER_TOOL_CALL = "tool.call"


# ---- structural dependencies ---------------------------------------------------------------------


class HubLike(Protocol):
    @property
    def url(self) -> str: ...
    def declare_services(self, client_id: str, services: Iterable[str]) -> None: ...
    async def request(
        self,
        client_id: str,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_stream: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]: ...


class TokenIssuer(Protocol):
    def worker_env(self, worker_id: str, *, ttl_s: float | None = None) -> Mapping[str, str]: ...


class PermissionEngineLike(Protocol):
    def check(self, request: PermissionRequest) -> Any: ...
    def active_profile(self) -> Profile: ...


class SecretReader(Protocol):
    def get(self, name: str) -> str | None: ...


class AuditSink(Protocol):
    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> int: ...


class JobLike(Protocol):
    def assign(self, pid: int) -> bool: ...


class ProcessLike(Protocol):
    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[[Sequence[str], Mapping[str, str]], ProcessLike]


class HealthRegistrar(Protocol):
    def add_check(self, check: Check) -> None: ...
    def remove_check(self, name: str) -> None: ...


# ---- tool registry (shape owned by src/nox/tools/registry.py) ------------------------------------


@dataclass(frozen=True, slots=True)
class PluginToolSpec:
    """Local stand-in with the exact shape of `nox.tools.registry.ToolSpec`.

    Used when that module is not importable yet (it is built in parallel); `resolve_tool_spec`
    prefers the real class whenever it exists so there is only ever one registry at runtime.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    risk: Risk
    side_effects: bool
    local: bool
    targets: Callable[[dict[str, Any]], str] | None
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolRegistryLike(Protocol):
    def register(self, spec: Any) -> Any: ...
    def get(self, name: str) -> Any: ...
    def names(self) -> list[str]: ...
    def unregister(self, name: str) -> None: ...


class LocalToolRegistry:
    """Minimal `ToolRegistry` used until `nox.tools.registry` is available."""

    def __init__(self) -> None:
        self._specs: dict[str, Any] = {}

    def register(self, spec: Any) -> Any:
        name = str(spec.name)
        if name in self._specs:
            raise ValueError(f"tool {name!r} is already registered")
        self._specs[name] = spec
        return spec

    def get(self, name: str) -> Any:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)


#: Environment variables a plugin worker process inherits from the core. Everything else stays in
#: the core: third-party plugin code has no business reading the whole environment of the process
#: that supervises it - the API keys of unrelated tools included. What remains is what CPython
#: itself needs in order to start on Windows: an interpreter path, a home directory and a
#: temporary directory.
_INHERITED_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SystemRoot",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "ALLUSERSPROFILE",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)


def _worker_environment() -> dict[str, str]:
    """The minimal environment a plugin worker is spawned with (see `_INHERITED_ENV`)."""
    return {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}


def _manifest_of(rec: PluginRecord) -> PluginManifest:
    """The record's manifest, as a checked invariant rather than an `assert`.

    Every record reachable from a request handler has one; `assert` would vanish under `python -O`
    and turn this into an `AttributeError` on the next line.
    """
    if rec.manifest is None:  # pragma: no cover - a record without a manifest is never routed
        raise IpcError(ERR_INTERNAL, f"plugin {rec.plugin_id!r} has no validated manifest")
    return rec.manifest


def _tools_module() -> Any | None:
    try:
        return importlib.import_module("nox.tools.registry")
    except ImportError:
        return None


def resolve_tool_registry() -> ToolRegistryLike:
    """The core's `ToolRegistry` when it exists, else the local fallback (never a silent stub)."""
    module = _tools_module()
    factory = getattr(module, "ToolRegistry", None) if module is not None else None
    if factory is None:
        log.info("plugins.tool_registry_fallback", note="nox.tools.registry not available")
        return LocalToolRegistry()
    return cast(ToolRegistryLike, factory())


def resolve_tool_spec() -> type[Any]:
    module = _tools_module()
    spec = getattr(module, "ToolSpec", None) if module is not None else None
    return cast(type[Any], spec) if spec is not None else PluginToolSpec


class PluginToolInput(BaseModel):
    """Core-side passthrough input model.

    The plugin worker owns the tool's real pydantic model and validates every call against it
    before the handler runs; the core only carries the declared JSON schema for the catalogue, so
    this model must not reject anything the worker would accept.
    """

    model_config = ConfigDict(extra="allow")


def _input_model_for(name: str, schema: Mapping[str, Any]) -> type[BaseModel]:
    model = create_model(
        "".join(part.capitalize() for part in name.split(".")) + "Input",
        __base__=PluginToolInput,
    )
    model.__doc__ = f"Declared input schema of the plugin tool {name!r} (validated in the worker)."
    model.__nox_declared_schema__ = dict(schema)  # type: ignore[attr-defined]
    return model


# ---- lifecycle -----------------------------------------------------------------------------------


class PluginState(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    ENABLED = "enabled"
    SPAWNED = "spawned"
    REGISTERED = "registered"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


#: States in which a plugin worker process is expected to exist.
_LIVE_STATES = frozenset(
    {PluginState.SPAWNED, PluginState.REGISTERED, PluginState.RUNNING, PluginState.STOPPING}
)


class PluginStatus(BaseModel):
    """Honest, serializable status of one plugin (dashboard/health)."""

    model_config = ConfigDict(frozen=True)
    plugin_id: str
    state: PluginState
    version: str = ""
    enabled: bool = False
    reason: str = ""
    tools: list[str] = Field(default_factory=list)
    restarts: int = 0
    pid: int | None = None


@dataclass
class PluginRecord:
    plugin_id: str
    path: Path
    state: PluginState = PluginState.DISCOVERED
    manifest: PluginManifest | None = None
    reason: str = ""
    enabled: bool = False
    history: list[PluginState] = field(default_factory=lambda: [PluginState.DISCOVERED])
    process: ProcessLike | None = None
    client_id: str | None = None
    tools: list[str] = field(default_factory=list)
    restarts: int = 0
    restart_times: list[float] = field(default_factory=list)
    stopping: bool = False
    monitor: asyncio.Task[None] | None = None
    registered: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def version(self) -> str:
        return self.manifest.version if self.manifest is not None else ""

    def status(self) -> PluginStatus:
        return PluginStatus(
            plugin_id=self.plugin_id,
            state=self.state,
            version=self.version,
            enabled=self.enabled,
            reason=self.reason,
            tools=list(self.tools),
            restarts=self.restarts,
            pid=self.process.pid if self.process is not None else None,
        )


class PluginManagerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    #: `plugins.enabled` from the configuration; only these ids are started.
    enabled: list[str] = Field(default_factory=list)
    restart_limit: int = Field(default=3, ge=0)
    restart_window_s: float = Field(default=300.0, gt=0.0)
    backoff_base_s: float = Field(default=2.0, ge=0.0)
    poll_interval_s: float = Field(default=0.5, gt=0.0)
    #: Plugin Architecture: no ack within 2 s -> terminate.
    stop_ack_timeout_s: float = Field(default=2.0, gt=0.0)
    terminate_timeout_s: float = Field(default=2.0, gt=0.0)
    register_timeout_s: float = Field(default=30.0, gt=0.0)
    tool_timeout_s: float = Field(default=30.0, gt=0.0)


# ---- IPC payloads --------------------------------------------------------------------------------


class PluginToolDeclaration(BaseModel):
    name: str
    description: str = ""
    risk: Risk = Risk.MEDIUM
    side_effects: bool = True
    local: bool = True
    input_schema: dict[str, Any] = Field(default_factory=dict)


class PluginRegister(BaseModel):
    plugin_id: str
    version: str = ""
    pid: int = 0
    tools: list[PluginToolDeclaration] = Field(default_factory=list)


class PluginSecretGet(BaseModel):
    name: str


class PluginToolCall(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


# ---- the manager ---------------------------------------------------------------------------------


class PluginManager:
    """Owns every plugin worker: discovery, validation, spawn, restart, stop, health, IPC."""

    def __init__(
        self,
        *,
        plugins_dir: Path,
        bus: EventBus,
        hub: HubLike,
        tokens: TokenIssuer,
        registry: RequestRegistry,
        engine: PermissionEngineLike,
        secrets: SecretReader,
        settings: PluginManagerSettings | None = None,
        audit: AuditSink | None = None,
        job: JobLike | None = None,
        health: HealthRegistrar | None = None,
        tool_registry: ToolRegistryLike | None = None,
        worker_command: Sequence[str] | None = None,
        cwd: Path | None = None,
        mode: Callable[[], str] = lambda: "companion",
        safe_mode: Callable[[], bool] = lambda: False,
        global_egress_allowlist: Sequence[str] = (),
        loopback_allowlist: Sequence[str] = (),
        process_factory: ProcessFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plugins_dir = plugins_dir
        self.settings = settings or PluginManagerSettings()
        self._bus = bus
        self._hub = hub
        self._tokens = tokens
        self._registry = registry
        self._engine = engine
        self._secrets = secrets
        self._audit = audit
        self._job = job
        self._health = health
        # `is not None`, not `or`: `ToolRegistry` defines `__len__`, so the core's registry is
        # falsy while it is still empty - which it always is at composition time, because the
        # built-in tools are registered later in the boot sequence. With `or`, every injected
        # registry was silently discarded and plugin tools were registered into a second, private
        # one, so `ToolExecutor` (and therefore every model- or dashboard-initiated plugin tool
        # call) reported `tool.unknown`.
        self.tools: ToolRegistryLike = (
            tool_registry if tool_registry is not None else resolve_tool_registry()
        )
        self._spec_class = resolve_tool_spec()
        self._worker_command = list(worker_command or [sys.executable, "-m", "nox.worker"])
        self._cwd = cwd
        self._mode = mode
        self._safe_mode = safe_mode
        self._global_egress = tuple(global_egress_allowlist)
        self._loopback = tuple(loopback_allowlist)
        self._process_factory = process_factory or self._default_process_factory
        self._clock = clock
        self._records: dict[str, PluginRecord] = {}
        self._closing = False
        self._handlers_registered = False
        self._unsubscribe: Callable[[], None] | None = None

    # -- public API --------------------------------------------------------------------------

    def records(self) -> dict[str, PluginRecord]:
        return dict(self._records)

    def status(self) -> list[PluginStatus]:
        return [rec.status() for rec in self._records.values()]

    def state_of(self, plugin_id: str) -> PluginState | None:
        rec = self._records.get(plugin_id)
        return rec.state if rec is not None else None

    def register_handlers(self) -> None:
        """Register the three core-side plugin requests (role `plugin` only). Idempotent."""
        if self._handlers_registered:
            return
        self._registry.register(
            "plugin.register", PluginRegister, self._h_register, roles=("plugin",)
        )
        self._registry.register(
            "plugin.secret.get", PluginSecretGet, self._h_secret_get, roles=("plugin",)
        )
        self._registry.register(
            "plugin.tool.call", PluginToolCall, self._h_tool_call, roles=("plugin",)
        )
        self._handlers_registered = True

    async def start(self) -> None:
        """Discover + validate every plugin, then spawn the enabled ones. Failures are isolated."""
        self._closing = False
        self.register_handlers()
        if self._unsubscribe is None:
            # AC 3: a profile switch must start/stop profile-gated plugins without a core restart.
            self._unsubscribe = self._bus.subscribe(E.SYSTEM_MODE_CHANGED, self._on_mode_changed)
        await self.discover()
        for rec in list(self._records.values()):
            if rec.enabled:
                await self._spawn(rec)

    async def discover(self) -> None:
        """`discovered -> validated -> enabled`; a bad manifest lands in `failed` and is skipped."""
        for path in discover_manifest_paths(self.plugins_dir):
            plugin_id = path.parent.name
            rec = self._records.get(plugin_id)
            if rec is not None and rec.state in _LIVE_STATES:
                continue
            rec = PluginRecord(plugin_id=plugin_id, path=path.parent)
            self._records[plugin_id] = rec
            self._register_health_check(rec)
            try:
                rec.manifest = load_manifest(path, expected_id=plugin_id)
                self._transition(rec, PluginState.VALIDATED)
                check_egress(
                    rec.manifest,
                    self._engine.active_profile(),
                    global_allowlist=self._global_egress,
                    loopback_allowlist=self._loopback,
                )
            except ManifestError as exc:
                await self._fail(rec, str(exc))
                continue
            self._apply_enablement(rec)

    async def stop(self, reason: str = "shutdown") -> None:
        """Graceful shutdown of every plugin; also stops the crash monitors."""
        self._closing = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.stop_all(reason)
        for rec in self._records.values():
            if rec.monitor is not None:
                rec.monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rec.monitor
                rec.monitor = None

    async def stop_all(
        self, reason: str = "kill_switch", *, ack_timeout_s: float | None = None
    ) -> None:
        """Kill-switch hook: `plugin.stop` to every worker, then terminate what has not stopped."""
        live = [rec for rec in self._records.values() if rec.state in _LIVE_STATES]
        if not live:
            return
        await asyncio.gather(
            *(self._stop_plugin(rec, reason, ack_timeout_s) for rec in live),
            return_exceptions=True,
        )

    async def _on_mode_changed(self, _event: Event) -> None:
        await self.apply_profile()

    async def apply_profile(self) -> None:
        """Re-apply the profile gate after a profile/mode switch: spawn newcomers, stop leavers.

        A plugin that already `failed` stays failed until a manual restart, and nothing is spawned
        while the kill switch is engaged (Plugin Architecture "Lifecycle").
        """
        profile = self._engine.active_profile()
        for rec in list(self._records.values()):
            manifest = rec.manifest
            if manifest is None or rec.state is PluginState.FAILED:
                continue
            wanted = manifest.matches_profile(profile.id) and (
                rec.plugin_id in self.settings.enabled
            )
            if wanted and rec.state in (PluginState.VALIDATED, PluginState.STOPPED):
                if self._safe_mode():
                    continue
                try:
                    check_egress(
                        manifest,
                        profile,
                        global_allowlist=self._global_egress,
                        loopback_allowlist=self._loopback,
                    )
                except ManifestError as exc:
                    await self._fail(rec, str(exc))
                    continue
                rec.enabled = True
                self._transition(rec, PluginState.ENABLED)
                await self._spawn(rec)
            elif not wanted and rec.state in _LIVE_STATES:
                rec.enabled = False
                await self._stop_plugin(rec, f"profile {profile.id!r} does not match", None)
                rec.reason = f"profile {profile.id!r} not in {manifest.profiles}"

    # -- health ------------------------------------------------------------------------------

    def health_of(self, plugin_id: str) -> tuple[HealthStatus, str]:
        """Honest per-plugin health: running = available, starting = limited, anything else
        unavailable."""
        rec = self._records.get(plugin_id)
        if rec is None:
            return HealthStatus.UNAVAILABLE, "unknown plugin"
        if rec.state is PluginState.RUNNING:
            if rec.process is not None and rec.process.poll() is not None:
                return HealthStatus.UNAVAILABLE, "worker process gone"
            return HealthStatus.AVAILABLE, f"running ({len(rec.tools)} tools)"
        if rec.state in (PluginState.SPAWNED, PluginState.REGISTERED, PluginState.STOPPING):
            return HealthStatus.LIMITED, rec.state.value
        if rec.state is PluginState.ENABLED:
            return HealthStatus.LIMITED, "enabled, not spawned yet"
        return HealthStatus.UNAVAILABLE, rec.reason or rec.state.value

    def health_checks(self) -> list[Check]:
        return [self._check_for(plugin_id) for plugin_id in sorted(self._records)]

    def _check_for(self, plugin_id: str) -> Check:
        async def probe() -> tuple[HealthStatus, str]:
            return self.health_of(plugin_id)

        return Check(f"plugin.{plugin_id}", probe, timeout_s=2.0)

    def _register_health_check(self, rec: PluginRecord) -> None:
        if self._health is None:
            return
        self._health.remove_check(f"plugin.{rec.plugin_id}")
        self._health.add_check(self._check_for(rec.plugin_id))

    # -- lifecycle internals ------------------------------------------------------------------

    def _transition(self, rec: PluginRecord, state: PluginState, reason: str = "") -> None:
        rec.state = state
        rec.reason = reason
        rec.history.append(state)
        log.info("plugin.state", plugin=rec.plugin_id, state=state.value, reason=reason)

    async def _fail(self, rec: PluginRecord, reason: str) -> None:
        self._transition(rec, PluginState.FAILED, reason)
        rec.enabled = False
        self._unregister_tools(rec)
        log.error("plugin.failed", plugin=rec.plugin_id, reason=reason)
        await self._publish(E.PLUGIN_FAILED, rec, reason)

    def _apply_enablement(self, rec: PluginRecord) -> None:
        """`enabled` = manifest profile matches the active profile AND the id is in
        `plugins.enabled`."""
        manifest = rec.manifest
        if manifest is None:
            return
        profile_id = self._engine.active_profile().id
        if rec.plugin_id not in self.settings.enabled:
            rec.enabled = False
            rec.reason = "not in plugins.enabled"
            return
        if not manifest.matches_profile(profile_id):
            rec.enabled = False
            rec.reason = f"profile {profile_id!r} not in {manifest.profiles}"
            return
        rec.enabled = True
        self._transition(rec, PluginState.ENABLED)

    def _default_process_factory(
        self, command: Sequence[str], env: Mapping[str, str]
    ) -> ProcessLike:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv built from config, no shell
            list(command), env=dict(env), cwd=str(self._cwd) if self._cwd else None
        )
        return cast(ProcessLike, proc)

    async def _spawn(self, rec: PluginRecord) -> None:
        manifest = rec.manifest
        if manifest is None:
            return
        client_id = f"{PLUGIN_CLIENT_PREFIX}{rec.plugin_id}"
        env = _worker_environment()
        env.update(self._tokens.worker_env(client_id))
        env["NOX_HUB_URL"] = self._hub.url
        env["NOX_PLUGINS_DIR"] = str(self.plugins_dir)
        command = [*self._worker_command, "--plugin", rec.plugin_id]
        rec.stopping = False
        rec.registered = asyncio.Event()
        try:
            rec.process = self._process_factory(command, env)
        except OSError as exc:
            await self._fail(rec, f"spawn failed: {exc}")
            return
        if self._job is not None:
            self._job.assign(rec.process.pid)
        self._transition(rec, PluginState.SPAWNED)
        log.info("plugin.spawned", plugin=rec.plugin_id, pid=rec.process.pid)
        rec.monitor = asyncio.create_task(
            self._monitor(rec), name=f"plugin-monitor-{rec.plugin_id}"
        )

    async def _monitor(self, rec: PluginRecord) -> None:
        """Watch the worker process; a crash triggers the backoff restart chain."""
        proc = rec.process
        if proc is None:
            return
        while True:
            await asyncio.sleep(self.settings.poll_interval_s)
            if self._closing or rec.stopping or rec.process is not proc:
                return
            code = proc.poll()
            if code is None:
                continue
            await self._on_crash(rec, code)
            return

    async def _on_crash(self, rec: PluginRecord, code: int) -> None:
        self._unregister_tools(rec)
        rec.client_id = None
        now = self._clock()
        window = self.settings.restart_window_s
        rec.restart_times = [t for t in rec.restart_times if now - t <= window]
        if len(rec.restart_times) >= self.settings.restart_limit:
            await self._fail(
                rec,
                f"worker exited with code {code}; "
                f"{self.settings.restart_limit} restarts within {window:g}s exhausted",
            )
            return
        attempt = len(rec.restart_times) + 1
        rec.restart_times.append(now)
        rec.restarts += 1
        reason = f"worker exited with code {code} (restart {attempt}/{self.settings.restart_limit})"
        self._transition(rec, PluginState.FAILED, reason)
        await self._publish(E.PLUGIN_FAILED, rec, reason)
        delay = self.settings.backoff_base_s * (2 ** (attempt - 1))
        log.warning(
            "plugin.restarting", plugin=rec.plugin_id, attempt=attempt, delay_s=round(delay, 2)
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        if self._closing or rec.stopping or self._safe_mode():
            return
        await self._spawn(rec)

    async def _stop_plugin(
        self, rec: PluginRecord, reason: str, ack_timeout_s: float | None
    ) -> None:
        rec.stopping = True
        self._transition(rec, PluginState.STOPPING, reason)
        timeout = ack_timeout_s or self.settings.stop_ack_timeout_s
        if rec.client_id is not None:
            try:
                await asyncio.wait_for(
                    self._hub.request(
                        rec.client_id, WORKER_STOP_REQUEST, {"reason": reason}, timeout=timeout
                    ),
                    timeout=timeout,
                )
            except (TimeoutError, IpcError, asyncio.CancelledError, OSError) as exc:
                log.warning("plugin.stop_not_acked", plugin=rec.plugin_id, error=type(exc).__name__)
        await self._terminate(rec)
        self._unregister_tools(rec)
        rec.client_id = None
        rec.registered.clear()
        self._transition(rec, PluginState.STOPPED, reason)
        await self._publish(E.PLUGIN_STOPPED, rec, reason)

    async def _terminate(self, rec: PluginRecord) -> None:
        proc = rec.process
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(proc.wait), timeout=self.settings.terminate_timeout_s
            )
        except TimeoutError:
            log.warning("plugin.terminate_timeout", plugin=rec.plugin_id)
            proc.kill()

    def _unregister_tools(self, rec: PluginRecord) -> None:
        for name in rec.tools:
            try:
                self.tools.unregister(name)
            except Exception as exc:  # noqa: BLE001 - one stuck name must not keep the others
                log.warning(
                    "plugin.tool_unregister_failed",
                    plugin=rec.plugin_id,
                    tool=name,
                    error=f"{type(exc).__name__}: {exc}",
                )
        rec.tools = []

    async def _publish(self, name: str, rec: PluginRecord, reason: str = "") -> None:
        payload = PluginLifecycle(
            plugin_id=rec.plugin_id, version=rec.version, reason=reason
        ).model_dump(mode="json")
        try:
            await self._bus.publish(Event(name=name, payload=payload, source="plugins"))
        except Exception as exc:  # noqa: BLE001 - a lifecycle event must never break the lifecycle
            log.warning("plugin.event_publish_failed", name=name, error=type(exc).__name__)

    # -- IPC handlers (role `plugin` only) ------------------------------------------------------

    def _record_for(self, ctx: RequestContext) -> PluginRecord:
        """The client id is bound to the one-time token the core issued, so it identifies the
        plugin."""
        if not ctx.client_id.startswith(PLUGIN_CLIENT_PREFIX):
            raise IpcError(ERR_PERMISSION, "not a plugin connection")
        plugin_id = ctx.client_id[len(PLUGIN_CLIENT_PREFIX) :]
        rec = self._records.get(plugin_id)
        if rec is None or rec.manifest is None:
            raise IpcError(ERR_NOT_FOUND, f"unknown plugin {plugin_id!r}")
        return rec

    async def _h_register(self, ctx: RequestContext, p: PluginRegister) -> dict[str, Any]:
        rec = self._record_for(ctx)
        manifest = _manifest_of(rec)
        if p.plugin_id != rec.plugin_id:
            raise IpcError(ERR_PERMISSION, "plugin_id does not match the connection")
        if rec.state not in (PluginState.SPAWNED, PluginState.REGISTERED, PluginState.RUNNING):
            raise IpcError(ERR_PERMISSION, f"plugin {rec.plugin_id!r} is {rec.state.value}")
        rec.client_id = ctx.client_id
        self._transition(rec, PluginState.REGISTERED)
        self._hub.declare_services(ctx.client_id, sorted(manifest.event_namespaces()))
        self._unregister_tools(rec)
        try:
            self._register_tools(rec, manifest, p.tools)
        except (ManifestError, ValueError) as exc:
            self._unregister_tools(rec)
            await self._fail(rec, f"tool registration rejected: {exc}")
            raise IpcError(ERR_PERMISSION, str(exc)) from exc
        self._transition(rec, PluginState.RUNNING)
        rec.registered.set()
        await self._publish(E.PLUGIN_STARTED, rec, f"{len(rec.tools)} tools")
        log.info("plugin.running", plugin=rec.plugin_id, tools=rec.tools, pid=p.pid)
        return {
            "ok": True,
            "config": dict(manifest.config),
            "profile": self._engine.active_profile().id,
            "tools": list(rec.tools),
        }

    def _register_tools(
        self,
        rec: PluginRecord,
        manifest: PluginManifest,
        declarations: Sequence[PluginToolDeclaration],
    ) -> None:
        for declaration in declarations:
            permission = validate_tool_name(manifest, declaration.name)
            if declaration.risk is not permission.risk:
                raise ManifestError(
                    f"tool {declaration.name!r} declares risk {declaration.risk.value!r} "
                    f"but the manifest says {permission.risk.value!r}"
                )
            spec = self._spec_class(
                name=declaration.name,
                description=declaration.description,
                input_model=_input_model_for(declaration.name, declaration.input_schema),
                risk=permission.risk,
                side_effects=declaration.side_effects,
                local=declaration.local,
                targets=None,
                handler=self._forwarding_handler(rec.plugin_id, declaration.name),
            )
            self.tools.register(spec)
            rec.tools.append(declaration.name)

    def _forwarding_handler(
        self, plugin_id: str, tool_name: str
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handler(payload: dict[str, Any]) -> dict[str, Any]:
            rec = self._records.get(plugin_id)
            if rec is None or rec.state is not PluginState.RUNNING or rec.client_id is None:
                raise IpcError(
                    ERR_UNAVAILABLE, f"plugin {plugin_id!r} is not running", retryable=True
                )
            return await self._hub.request(
                rec.client_id,
                WORKER_TOOL_CALL,
                {"name": tool_name, "input": payload},
                timeout=self.settings.tool_timeout_s,
            )

        return handler

    async def _h_secret_get(self, ctx: RequestContext, p: PluginSecretGet) -> dict[str, Any]:
        rec = self._record_for(ctx)
        manifest = _manifest_of(rec)
        if p.name not in manifest.secrets:
            self._audit_secret(rec.plugin_id, p.name, "deny", "denied")
            log.warning("plugin.secret_denied", plugin=rec.plugin_id)
            raise IpcError(
                ERR_PERMISSION, f"secret {p.name!r} is not declared by plugin {rec.plugin_id!r}"
            )
        try:
            value = self._secrets.get(p.name)
        except IpcError:
            raise
        except Exception as exc:  # noqa: BLE001 - a keyring failure is reported, never faked
            self._audit_secret(rec.plugin_id, p.name, "allow", "failed")
            raise IpcError(ERR_INTERNAL, f"secret store error: {type(exc).__name__}") from exc
        self._audit_secret(rec.plugin_id, p.name, "allow", "ok" if value else "not_found")
        if value is None:
            raise IpcError(ERR_NOT_FOUND, f"secret {p.name!r} is not set in the credential manager")
        return {"name": p.name, "value": value}

    def _audit_secret(self, plugin_id: str, name: str, decision: str, result: str) -> None:
        """Record one secret access on the audit chain.

        Raises `IpcError` when the chain cannot record it: a secret handed to a plugin with no
        trace of it having happened is worse than a refused one.
        """
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor=f"plugin:{plugin_id}",
                tool="secrets",
                action="secret.get",
                target=name,  # the name, never the value
                decision=decision,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - converted into a refusal below
            log.error(
                "plugin.secret_audit_failed",
                plugin=plugin_id,
                secret=name,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise IpcError(
                ERR_INTERNAL, "secret access could not be audited, so it is refused"
            ) from exc

    async def _h_tool_call(self, ctx: RequestContext, p: PluginToolCall) -> dict[str, Any]:
        self._record_for(ctx)  # only an authenticated plugin may reach the tool bus
        spec = self.tools.get(p.name)
        if spec is None:
            raise IpcError(ERR_NOT_FOUND, f"unknown tool {p.name!r}")
        tool, _, action = p.name.partition(".")
        decision = self._engine.check(
            PermissionRequest(
                agent="plugin",
                tool=tool,
                action=action,
                mode=self._mode(),
                risk=Risk(spec.risk),
                target=str(p.input.get("target", "")),
                origin="plugin",
            )
        )
        if decision.decision is Decision.DENY:
            raise IpcError(ERR_PERMISSION, f"{p.name}: {decision.reason or decision.rule_id}")
        if decision.decision is Decision.CONFIRM:
            # No plugin-initiated confirmation flow exists yet; reported honestly, never faked.
            raise IpcError(
                ERR_PERMISSION,
                f"{p.name} requires confirmation ({decision.rule_id}); "
                "plugin-initiated confirmations are not implemented",
            )
        result = await spec.handler(dict(p.input))
        return {"ok": True, "result": dict(result or {})}
