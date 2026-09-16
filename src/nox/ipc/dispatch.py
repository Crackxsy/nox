"""Request registry and role-based dispatch for the IPC hub (IPC Model §Requests, §Security).

A request is accepted when (1) a handler is registered for its name, (2) the caller's role
allow-list (or a service namespace declared for the connection) matches the name, and (3) the
handler's own
allowed_roles - if given - contains the role. Payloads are validated against the registered pydantic
model before the handler runs. Failures become ErrorPayload codes: validation.failed, not_found,
permission.denied, internal.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

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

# ---- name globbing ------------------------------------------------------------


def match_name(pattern: str, name: str) -> bool:
    """Dotted glob: `*` matches one segment (fnmatch inside it), `**` any number of segments."""
    return _match_segments(pattern.split("."), name.split("."))


def _match_segments(pat: list[str], parts: list[str]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_match_segments(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0], head) and _match_segments(rest, parts[1:])


def matches_any(patterns: Iterable[str], name: str) -> bool:
    return any(match_name(p, name) for p in patterns)


# ---- role allow-lists (IPC Model §Security rules) ----------------------------------------

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
    "health.history",  # ST-08: health-history panel (nox.data.repos.HealthHistoryRepository)
    "config.effective",  # ST-08: Settings view, read-only redacted NoxConfig
    # EPIC-21 `nox.settings`: the writable half of the Settings page. Each handler re-checks its
    # own roles at registration time (`roles=("shell", "dashboard")`); this list only says that
    # the name is reachable for a UI role at all.
    "config.get",
    "config.set",
    "secrets.status",
    "secrets.set",
    "secrets.delete",
    "twitch.auth.start",
    "twitch.auth.status",
    "twitch.auth.disconnect",
    "personality.get",
    "personality.set",
)

ROLE_ALLOWLIST: Mapping[str, tuple[str, ...]] = {
    "pet": ("ipc.*", "pet.interact", "state.get"),
    "dashboard": _DASHBOARD,
    "shell": (*_DASHBOARD, "voice.ptt", "security.permission.reply"),
    "worker": ("ipc.*", "worker.*"),  # plus declared service namespaces per connection
    # ST-11-01: `plugin.**` covers plugin.register / plugin.secret.get / plugin.tool.call;
    # `state.get` is the read-only state view of the Plugin API (filtered by role in the handler).
    "plugin": ("ipc.*", "worker.*", "plugin.**", "state.get"),
    # core, supervisor and remote never authenticate over the hub in v0.1.
    "core": (),
    "supervisor": (),
    "remote": (),
}


def service_patterns(services: Iterable[str]) -> tuple[str, ...]:
    """Request/event patterns a worker gains by declaring service names ("stt" -> "stt.**")."""
    return tuple(f"{s}.**" for s in services)


def role_allows(role: str, name: str, extra_patterns: Iterable[str] = ()) -> bool:
    return matches_any(ROLE_ALLOWLIST.get(role, ()), name) or matches_any(extra_patterns, name)


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
        reg = self._handlers.get(name)
        if reg is None:
            return False
        if not role_allows(role, name, extra_patterns):
            return False
        return reg.allowed_roles is None or role in reg.allowed_roles

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
