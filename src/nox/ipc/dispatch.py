"""The request registry, and the role check in front of it.

A request is accepted when all three hold: a handler is registered for its name, the caller's role
allow-list (or a service namespace the connection declared) matches the name, and the handler's
own `roles` - if it named any - contains the caller's role. While synthetic input or a node
session is in play, `nox.security.access.AccessPolicy` then decides whether a UI client may make a
request that changes something. The payload is validated against the handler's model before it
runs. Every failure becomes one error code: `validation.failed`, `not_found`,
`permission.denied` or `internal`.

Name matching is the dotted, segment-aware dialect from `nox.core.globbing`: `security.*` covers
`security.kill` but not `security.permission.reply`, which is why the latter is listed by name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from nox.core.globbing import name_matches, name_matches_any
from nox.ipc._log import get_logger
from nox.ipc.errors import (
    CORE_SOURCE,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_VALIDATION,
    IpcError,
)
from nox.ipc.protocol import NAME_ERROR, RESPONSE_PAYLOAD_MODELS, Envelope, Kind
from nox.ipc.roles import DEVICE_ROLE, UI_ROLES
from nox.ipc.tokens import DEVICE_CLIENT_PREFIX
from nox.security.access import (
    AccessPolicy,
    Allow,
    ClientContext,
    ClientKind,
    peer_class_of,
)

log = get_logger(__name__)


def match_name(pattern: str, name: str) -> bool:
    """Dotted glob match. Kept here because every IPC caller reads pattern first, name second."""
    return name_matches(name, pattern)


# ---- role allow-lists ---------------------------------------------------------------------
#
# The coarse surface per role, used for a request that was registered without its own `roles`, and
# for the namespaces a worker or plugin declares. A registered handler's own `roles` wins; see
# `RequestRegistry.is_allowed`.

_DASHBOARD: tuple[str, ...] = (
    "ipc.*",
    "state.get",
    "health.get",
    "ai.providers",
    "mode.set",
    "privacy.set",
    "security.*",  # security.kill, security.panic (one segment: not security.permission.reply)
    "voice.mute",
    "chat.send",
    "health.history",  # the health-history panel
    "config.effective",  # the Settings view: read-only, redacted configuration
    # The writable half of the Settings page. Each handler re-checks its own roles at registration
    # time; this list only says the name is reachable for a UI role at all.
    "config.get",
    "config.set",
    # Read-only `{"configured": bool}`. Two segments after `security.`, so the `security.*` entry
    # above does not cover it and it is listed by name, as `security.permission.reply` is for the
    # shell role.
    "security.pin.status",
    "secrets.status",
    "secrets.set",
    "secrets.delete",
    "twitch.auth.start",
    "twitch.auth.status",
    "twitch.auth.disconnect",
    "personality.get",
    "personality.set",
    "stream.session.status",
    "stream.funken.top",
    "plugin.status",
)

ROLE_ALLOWLIST: Mapping[str, tuple[str, ...]] = {
    "pet": ("ipc.*", "pet.interact", "state.get"),
    # The stream overlay (a browser source in OBS): it reads, it never asks for anything.
    "overlay": ("ipc.*", "state.get"),
    "dashboard": _DASHBOARD,
    "shell": (*_DASHBOARD, "voice.ptt", "security.permission.reply"),
    # A paired device in the home network (`nox.lan`) reaches only requests whose handler names it
    # (`nox.ipc.roles.UI_ROLES`). Nothing falls back to a broad list for it: a request registered
    # without roles is refused to a device.
    "device": ("ipc.*",),
    "worker": ("ipc.*", "worker.*"),  # plus the service namespaces the connection declares
    # `plugin.**` covers plugin.register / plugin.secret.get / plugin.tool.call; `state.get` is
    # the read-only state view of the plugin API, filtered by role inside the handler.
    #
    # `worker.*` is deliberately NOT here, only the liveness ping every worker process sends. A
    # plugin that could call `worker.register` was able to claim the `voice` service, which hands
    # it the speech routing and returns the voice configuration. A plugin registers through
    # `plugin.register`, which is scoped to its own manifest.
    "plugin": ("ipc.*", "plugin.**", "state.get", "worker.heartbeat"),
    # The MCP bridge: nothing by pattern. Its two requests (`mcp.tools.list`, `mcp.tools.call`)
    # are registered with `roles=("mcp",)` next to their handlers in `nox.mcp.gate`.
    "mcp": (),
    # core, supervisor, remote and node never authenticate over the hub. A node's whole surface
    # is the JSON-RPC allow-list on its own listener (`nox.node.protocol.NODE_TO_CORE`).
    "core": (),
    "supervisor": (),
    "remote": (),
    "node": (),
}


def service_patterns(services: Iterable[str]) -> tuple[str, ...]:
    """Request/event patterns a worker gains by declaring service names ("stt" -> "stt.**")."""
    return tuple(f"{s}.**" for s in services)


def role_allows(role: str, name: str, extra_patterns: Iterable[str] = ()) -> bool:
    return name_matches_any(name, ROLE_ALLOWLIST.get(role, ())) or name_matches_any(
        name, extra_patterns
    )


# ---- registry -----------------------------------------------------------------------------------

StreamFn = Callable[[dict[str, Any], bool], Awaitable[None]]
RequestHandler = Callable[["RequestContext", Any], Awaitable[BaseModel | Mapping[str, Any] | None]]


async def _no_stream(_payload: dict[str, Any], _done: bool) -> None:
    raise IpcError(ERR_INTERNAL, "streaming is not available in this context")


def client_context(client_id: str, role: str, peer: str = "") -> ClientContext:
    """Who is asking, for `AccessPolicy`: what the role makes the client (a local UI, a paired
    device, anything else), the device behind it and where it connects from. The hub builds it
    once per connection."""
    if role == DEVICE_ROLE:
        kind, device_id = ClientKind.DEVICE, client_id.removeprefix(DEVICE_CLIENT_PREFIX)
    elif role in UI_ROLES:
        # Derived from `UI_ROLES`, not from a list of its parts: a UI role added there is held to
        # the locks at once, never silently treated as "anything else".
        kind, device_id = ClientKind.LOCAL_UI, ""
    else:
        kind, device_id = ClientKind.OTHER, ""
    return ClientContext(client_id, role, kind, device_id, peer, peer_class_of(peer))


@dataclass
class RequestContext:
    """What a handler knows about the caller; `stream(payload, done)` sends a stream frame."""

    client_id: str
    role: str
    request: Envelope
    services: frozenset[str] = field(default_factory=frozenset)
    stream: StreamFn = _no_stream
    #: The network address a paired device connects from (bridged in by the network listener);
    #: "" for local clients.
    peer: str = ""
    #: The connection's own `ClientContext`, built once by the hub; built here for a caller that
    #: has none (a test, an in-process call).
    connection: InitVar[ClientContext | None] = None
    #: Who is asking, as `AccessPolicy` sees it.
    client: ClientContext = field(init=False)

    def __post_init__(self, connection: ClientContext | None) -> None:
        self.client = connection or client_context(self.client_id, self.role, self.peer)


@dataclass(frozen=True)
class Registration:
    name: str
    payload_model: type[BaseModel]
    handler: RequestHandler
    allowed_roles: frozenset[str] | None


class EmptyPayload(BaseModel):
    """For requests without parameters (`{}`)."""


class RequestRegistry:
    """name -> (payload model, async handler, allowed roles). Dispatches envelopes to handlers."""

    def __init__(self, policy: AccessPolicy | None = None) -> None:
        self._handlers: dict[str, Registration] = {}
        #: Who may make a request that changes something while synthetic input or a node
        #: session is in play. The desktop and node extensions install their conditions here.
        self.policy = policy or AccessPolicy()

    def register(
        self,
        name: str,
        payload_model: type[BaseModel],
        handler: RequestHandler,
        *,
        roles: Iterable[str] | None = None,
    ) -> None:
        if name in self._handlers:
            raise ValueError(f"handler for {name!r} already registered")
        allowed = None if roles is None else frozenset(roles)
        self._handlers[name] = Registration(name, payload_model, handler, allowed)

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def get(self, name: str) -> Registration | None:
        return self._handlers.get(name)

    def is_allowed(self, name: str, role: str, extra_patterns: Iterable[str] = ()) -> bool:
        """Whether `role` may call `name`.

        The roles a handler was registered with are the answer when it named any: they sit next to
        the handler, so they cannot drift away from it. A handler that named none falls back to
        `ROLE_ALLOWLIST` and to the service namespaces the connection declared.

        This used to be an *and* of both lists, which meant every UI request had to be added in
        two places - and a request that was only added in one of them was registered, reachable in
        the code, and refused at the door. That is how the whole stream view became unreachable.
        """
        reg = self._handlers.get(name)
        if reg is None:
            return False
        if reg.allowed_roles is not None:
            return role in reg.allowed_roles
        return role_allows(role, name, extra_patterns)

    async def dispatch(self, ctx: RequestContext, envelope: Envelope) -> Envelope:
        """Run the handler for `envelope`; always returns exactly one response or error envelope."""
        try:
            payload = await self._run(ctx, envelope)
        except IpcError as exc:
            if exc.code == ERR_INTERNAL:
                log.error("request_failed", name=envelope.name, client=ctx.client_id, code=exc.code)
            else:
                log.debug(
                    "request_rejected", name=envelope.name, client=ctx.client_id, code=exc.code
                )
            return envelope.reply(NAME_ERROR, exc.to_payload(), CORE_SOURCE, kind=Kind.ERROR)
        return envelope.reply(envelope.name, payload, CORE_SOURCE)

    async def _run(self, ctx: RequestContext, envelope: Envelope) -> dict[str, Any]:
        if envelope.kind is not Kind.REQUEST:
            raise IpcError(ERR_VALIDATION, f"expected kind=request, got {envelope.kind.value}")
        reg = self._handlers.get(envelope.name)
        if reg is None:
            raise IpcError(ERR_NOT_FOUND, f"unknown request {envelope.name!r}")
        if not self.is_allowed(envelope.name, ctx.role, service_patterns(ctx.services)):
            raise IpcError(ERR_PERMISSION, f"role {ctx.role!r} may not call {envelope.name!r}")
        payload = envelope.payload
        verdict = await self.policy.decide(ctx.client, envelope.name, payload)
        if not isinstance(verdict, Allow):
            # `sub_reason`: a stable code the dashboard words itself; the message is the English
            # fallback.
            raise IpcError(ERR_PERMISSION, verdict.reason, details=verdict.wire_details())
        if (
            verdict.pin_read
            and isinstance(payload, dict)
            and isinstance(payload.get("pin"), str)
            and "pin" not in reg.payload_model.model_fields
        ):
            # The PIN was for the check only; the request itself takes none.
            payload = {k: v for k, v in payload.items() if k != "pin"}
        try:
            model = reg.payload_model.model_validate(payload)
        except ValidationError as exc:
            raise IpcError(
                ERR_VALIDATION,
                f"invalid payload for {envelope.name!r}",
                details={"errors": _compact_errors(exc)},
            ) from None
        try:
            result = await reg.handler(ctx, model)
        except IpcError:
            raise
        except Exception as exc:
            log.exception("handler_crashed", name=envelope.name, client=ctx.client_id)
            raise IpcError(
                ERR_INTERNAL,
                f"handler for {envelope.name!r} failed",
                details={"type": type(exc).__name__},
            ) from exc
        payload = _to_payload(result)
        response_model = RESPONSE_PAYLOAD_MODELS.get(envelope.name)
        if response_model is not None:
            try:
                response_model.model_validate(payload)
            except ValidationError:
                log.error("response_validation_failed", name=envelope.name, client=ctx.client_id)
                raise IpcError(
                    ERR_INTERNAL, f"handler for {envelope.name!r} returned an invalid response"
                ) from None
        return payload


def _to_payload(result: BaseModel | Mapping[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return dict(result)


def _compact_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Field locations and messages only - never the offending input values."""
    return [
        {"loc": [str(x) for x in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_input=False, include_url=False)
    ]
