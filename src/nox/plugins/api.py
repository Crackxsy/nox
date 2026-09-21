"""`PluginApi`: the surface a plugin sees inside its worker process (Plugin Architecture "Plugin
API").

Everything the plugin can reach is scoped by its own manifest: `events.emit` only names in `emits`,
`events.on` only patterns in `listens`, `tools.register` only tools inside the `<id>.` namespace
that the manifest declared, `secrets.get` only declared `nox/<id>/...` names (the core reads the
keyring, the value is never persisted here), `state.get` a read-only IPC view, and `http` an
`httpx.AsyncClient` behind a per-plugin `EgressGuard` limited to `network.egress`. Nothing in this
module can widen what the core already validated.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.ipc.protocol import Envelope
from nox.plugins.manifest import (
    ManifestError,
    PluginManifest,
    split_endpoint,
    validate_secret_name,
    validate_tool_name,
)
from nox.security.egress import (
    EgressDecision,
    EgressGuard,
    entry_matches,
    is_loopback,
    loopback_entry_matches,
)
from nox.security.model import Profile, Risk

SECRET_GET = "plugin.secret.get"  # noqa: S105 - an IPC request name, not a secret
STATE_GET = "state.get"


class PluginApiError(RuntimeError):
    """The plugin asked for something its manifest does not declare."""


class PluginClient(Protocol):
    """The subset of `nox.ipc.client.IpcClient` the plugin API needs (structural: fakes in
    tests)."""

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]: ...
    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None: ...
    def on(self, name_glob: str, handler: Any) -> Callable[[], None]: ...


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ToolHandler = Callable[[Any], Awaitable[Mapping[str, Any]]]


# ---- privacy view --------------------------------------------------------------------------------


class PrivacyView:
    """The worker's copy of the core's privacy mode; the scoped egress guard reads it."""

    def __init__(self, mode: PrivacyMode | str = PrivacyMode.BALANCED) -> None:
        self._mode = PrivacyMode(mode)

    @property
    def mode(self) -> PrivacyMode:
        return self._mode

    def set(self, mode: PrivacyMode | str) -> None:
        self._mode = PrivacyMode(mode)


# ---- scoped egress guard ---------------------------------------------------------------


class PluginEgressGuard(EgressGuard):
    """`EgressGuard` scoped to one manifest's `network.egress`.

    The core authorized the list against the active profile before the worker was spawned; here it
    is the *upper* bound: an endpoint that is not declared is denied before the inherited
    privacy-mode rules (OFFLINE/PRIVATE -> allow-listed loopback only) even run.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        allowlist: Sequence[str],
        privacy: PrivacyView,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        declared = [entry.strip().lower() for entry in allowlist if entry.strip()]
        loopback = [e for e in declared if is_loopback(split_endpoint(e)[0])]
        profile = Profile(
            id=f"plugin:{plugin_id}",
            description=f"manifest-scoped egress for plugin {plugin_id}",
            rules=[],
            cloud_allowed=False,  # the manifest list is the only source, never the global one
            egress_allowlist=list(declared),
            loopback_allowlist=list(loopback),
        )
        super().__init__(
            profile=lambda: profile,
            privacy=privacy,
            global_allowlist=(),
            loopback_allowlist=(),
            transport_factory=transport_factory,
        )
        self.plugin_id = plugin_id
        self.declared = tuple(declared)

    def _declared(self, host: str, port: int) -> bool:
        for entry in self.declared:
            match = loopback_entry_matches if is_loopback(host) else entry_matches
            if match(entry, host, port):
                return True
        return False

    def check(self, host: str, port: int) -> EgressDecision:
        if not self._declared(host.strip().lower().rstrip("."), port):
            return EgressDecision(
                allowed=False,
                rule_id=f"plugin.{self.plugin_id}.not_declared",
                reason="host:port is not in the plugin manifest's network.egress list",
            )
        return super().check(host, port)


# ---- tools ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A tool the plugin registered; the core mirrors it into its `ToolRegistry`."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    risk: Risk
    side_effects: bool
    local: bool

    def declaration(self) -> dict[str, Any]:
        """What `plugin.register` sends to the core (no callables, no schemas the hub can't
        serialize)."""
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "side_effects": self.side_effects,
            "local": self.local,
            "input_schema": self.input_model.model_json_schema(),
        }


class PluginToolsApi:
    def __init__(self, manifest: PluginManifest) -> None:
        self._manifest = manifest
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
        risk: Risk | str,
        *,
        description: str = "",
        side_effects: bool | None = None,
        local: bool = True,
    ) -> RegisteredTool:
        """Expose a tool to the core's tool registry. Namespace and risk come from the manifest."""
        try:
            permission = validate_tool_name(self._manifest, name)
        except ManifestError as exc:
            raise PluginApiError(str(exc)) from exc
        if name in self._tools:
            raise PluginApiError(f"tool {name!r} is already registered")
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise PluginApiError(f"input_model for {name!r} must be a pydantic BaseModel subclass")
        wanted = Risk(risk)
        if wanted is not permission.risk:
            raise PluginApiError(
                f"tool {name!r} is declared with risk {permission.risk.value!r} in the manifest; "
                f"registering it as {wanted.value!r} is not allowed"
            )
        tool = RegisteredTool(
            name=name,
            description=description,
            input_model=input_model,
            handler=handler,
            risk=permission.risk,
            side_effects=permission.risk is not Risk.READ if side_effects is None else side_effects,
            local=local,
        )
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def declarations(self) -> list[dict[str, Any]]:
        return [self._tools[name].declaration() for name in self.names()]

    async def call(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate `payload` against the tool's input model and run its handler."""
        tool = self._tools.get(name)
        if tool is None:
            raise PluginApiError(f"unknown tool {name!r}")
        try:
            data = tool.input_model.model_validate(dict(payload))
        except ValidationError as exc:
            raise PluginApiError(f"invalid input for {name!r}: {exc.errors()[0]['msg']}") from None
        result = await tool.handler(data)
        return dict(result or {})


# ---- events / state / secrets --------------------------------------------------------------------


class PluginEventsApi:
    def __init__(self, manifest: PluginManifest, client: PluginClient) -> None:
        self._manifest = manifest
        self._client = client

    async def emit(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        if name not in self._manifest.events.emits:
            raise PluginApiError(
                f"event {name!r} is not in the manifest's events.emits for "
                f"plugin {self._manifest.id!r}"
            )
        await self._client.send_event(name, dict(payload or {}))

    def on(self, pattern: str, handler: EventHandler) -> Callable[[], None]:
        if pattern not in self._manifest.events.listens:
            raise PluginApiError(
                f"event pattern {pattern!r} is not in the manifest's events.listens for "
                f"plugin {self._manifest.id!r}"
            )

        async def _forward(env: Envelope) -> None:
            result = handler(env.name, dict(env.payload))
            if result is not None:
                await result

        return self._client.on(pattern, _forward)


class PluginStateApi:
    """Read-only view of the non-private state subtree (the core filters by role)."""

    def __init__(self, client: PluginClient, *, timeout_s: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout_s

    async def get(self, path: str | None = None) -> Any:
        response = await self._client.request(
            STATE_GET, {"path": path} if path else {}, timeout=self._timeout
        )
        if path:
            return response.get("value")
        return response


class PluginSecretsApi:
    """`nox/<id>/...` names only; the core reads the keyring, the value stays in memory here."""

    def __init__(
        self, manifest: PluginManifest, client: PluginClient, *, timeout_s: float = 5.0
    ) -> None:
        self._manifest = manifest
        self._client = client
        self._timeout = timeout_s

    async def get(self, name: str) -> str | None:
        try:
            validate_secret_name(self._manifest, name)
        except ManifestError as exc:
            raise PluginApiError(str(exc)) from exc
        response = await self._client.request(SECRET_GET, {"name": name}, timeout=self._timeout)
        value = response.get("value")
        return None if value is None else str(value)


# ---- the API object ------------------------------------------------------------------------------


class PluginApi:
    """What `create(plugin_api)` receives. One instance per plugin worker process."""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        client: PluginClient,
        config: Mapping[str, Any] | None = None,
        privacy: PrivacyView | None = None,
        logger: Any = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        request_timeout_s: float = 5.0,
    ) -> None:
        self.manifest = manifest
        self.plugin_id = manifest.id
        self.config: dict[str, Any] = dict(config if config is not None else manifest.config)
        self.log = logger or get_logger(f"nox.plugin.{manifest.id}")
        self.privacy = privacy or PrivacyView()
        self.events = PluginEventsApi(manifest, client)
        self.tools = PluginToolsApi(manifest)
        self.state = PluginStateApi(client, timeout_s=request_timeout_s)
        self.secrets = PluginSecretsApi(manifest, client, timeout_s=request_timeout_s)
        self.egress = PluginEgressGuard(
            plugin_id=manifest.id,
            allowlist=manifest.network.egress,
            privacy=self.privacy,
            transport_factory=transport_factory,
        )

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        """An `httpx.AsyncClient` whose every request passes this plugin's scoped egress guard."""
        if not self.manifest.network.egress:
            raise PluginApiError(
                f"plugin {self.plugin_id!r} declares no network.egress; http() is unavailable"
            )
        return self.egress.client(**kwargs)
