"""The request registry, and the role check in front of it.

A request is accepted when all three hold: a handler is registered for its name, the caller's role
allow-list (or a service namespace the connection declared) matches the name, and the handler's
own `roles` - if it named any - contains the caller's role. The payload is then validated against
the handler's model before it runs. Every failure becomes one error code: `validation.failed`,
`not_found`, `permission.denied` or `internal`.

Name matching is the dotted, segment-aware dialect from `nox.core.globbing`: `security.*` covers
`security.kill` but not `security.permission.reply`, which is why the latter is listed by name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
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
    "dashboard": _DASHBOARD,
    "shell": (*_DASHBOARD, "voice.ptt", "security.permission.reply"),
    "worker": ("ipc.*", "worker.*"),  # plus the service namespaces the connection declares
    # `plugin.**` covers plugin.register / plugin.secret.get / plugin.tool.call; `state.get` is
    # the read-only state view of the plugin API, filtered by role inside the handler.
    #
    # `worker.*` is deliberately NOT here, only the liveness ping every worker process sends. A
    # plugin that could call `worker.register` was able to claim the `voice` service, which hands
    # it the speech routing and returns the voice configuration. A plugin registers through
    # `plugin.register`, which is scoped to its own manifest.
    "plugin": ("ipc.*", "plugin.**", "state.get", "worker.heartbeat"),
    # core, supervisor and remote never authenticate over the hub.
    "core": (),
    "supervisor": (),
    "remote": (),
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


@dataclass
class RequestContext:
    """What a handler knows about the caller; `stream(payload, done)` sends a stream frame."""

    client_id: str
    role: str
    request: Envelope
    services: frozenset[str] = field(default_factory=frozenset)
    stream: StreamFn = _no_stream


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

    def __init__(self) -> None:
        self._handlers: dict[str, Registration] = {}

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
        try:
            model = reg.payload_model.model_validate(envelope.payload)
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
