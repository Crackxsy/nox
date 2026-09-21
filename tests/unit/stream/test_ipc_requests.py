"""Request-level tests for `stream.session.status` and `stream.funken.top`: role permission and
response shape, exercised through the real `RequestRegistry` role-check path rather than against
the `nox.stream` service objects in isolation.

Both requests are registered by `nox.ipc.handlers.core.CoreHandlers` for the two UI roles. The
handlers only touch `core.stream_sessions` and `core.funken_booking`, so they run here against a
tiny stub with those two attributes instead of a full core.

They were unreachable until the role check was fixed: they carried `roles=("shell", "dashboard")`
at registration, but the coarse per-role table in `nox.ipc.dispatch` listed neither name, and
`is_allowed` required both. The dashboard's stream page could therefore never load. The
registration's own roles are the authority now, and these tests fail if that regresses.
"""

from __future__ import annotations

from typing import Any, cast

from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION
from nox.ipc.handlers.core import CoreHandlers, FunkenTopRequest
from nox.ipc.protocol import Envelope, Kind, Source

ROLES = ("shell", "dashboard")


class _StubSessions:
    def status(self) -> dict[str, Any]:
        return {
            "active": True,
            "session_id": "42",
            "started_at": "2026-09-13T20:00:00+00:00",
            "scene": "Just Chatting",
            "plugins": {"obs": "connected", "twitch": "connected"},
        }


class _StubBooking:
    def top(self, limit: int = 10) -> list[dict[str, Any]]:
        return [
            {"viewer_id": "v1", "display_name": "Alice", "balance": 120.0, "tier": "gold"},
            {"viewer_id": "v2", "display_name": "Bob", "balance": 30.0, "tier": "bronze"},
        ][:limit]


class _StubCore:
    def __init__(self) -> None:
        self.stream_sessions = _StubSessions()
        self.funken_booking = _StubBooking()


def build_registry() -> RequestRegistry:
    """Register the two handlers exactly as the core does, bound to a stub."""
    handlers = CoreHandlers(cast("Any", _StubCore()))
    reg = RequestRegistry()
    reg.register("stream.session.status", EmptyPayload, handlers.stream_session_status, roles=ROLES)
    reg.register("stream.funken.top", FunkenTopRequest, handlers.stream_funken_top, roles=ROLES)
    return reg


def _req(name: str, payload: dict[str, Any], role: str) -> Envelope:
    return Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role=role, id=f"{role}:1"), payload=payload
    )  # type: ignore[arg-type]


def _ctx(env: Envelope) -> RequestContext:
    return RequestContext(client_id=env.src.id, role=env.src.role, request=env)


async def test_stream_session_status_allowed_for_dashboard_and_shell() -> None:
    reg = build_registry()
    for role in ROLES:
        env = _req("stream.session.status", {}, role=role)
        reply = await reg.dispatch(_ctx(env), env)
        assert reply.kind is Kind.RESPONSE, (role, reply.payload)
        assert reply.payload == {
            "active": True,
            "session_id": "42",
            "started_at": "2026-09-13T20:00:00+00:00",
            "scene": "Just Chatting",
            "plugins": {"obs": "connected", "twitch": "connected"},
        }


async def test_stream_funken_top_allowed_for_dashboard_and_shell() -> None:
    reg = build_registry()
    for role in ROLES:
        env = _req("stream.funken.top", {"limit": 1}, role=role)
        reply = await reg.dispatch(_ctx(env), env)
        assert reply.kind is Kind.RESPONSE, (role, reply.payload)
        assert reply.payload == {
            "viewers": [
                {"viewer_id": "v1", "display_name": "Alice", "balance": 120.0, "tier": "gold"}
            ]
        }


async def test_stream_funken_top_default_limit() -> None:
    reg = build_registry()
    env = _req("stream.funken.top", {}, role="dashboard")
    reply = await reg.dispatch(_ctx(env), env)
    assert reply.kind is Kind.RESPONSE
    assert [v["viewer_id"] for v in reply.payload["viewers"]] == ["v1", "v2"]


async def test_stream_requests_denied_for_non_ui_roles() -> None:
    reg = build_registry()
    for role in ("pet", "worker", "plugin"):
        for name, payload in (
            ("stream.session.status", {}),
            ("stream.funken.top", {}),
        ):
            env = _req(name, payload, role=role)
            reply = await reg.dispatch(_ctx(env), env)
            assert reply.kind is Kind.ERROR, (role, name)
            assert reply.payload["code"] == ERR_PERMISSION, (role, name)
